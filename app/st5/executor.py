"""Исполнитель пары для ST5 — sandbox / БОЕВОЙ (реальные деньги) через T-Bank.

Переиспользует REST-методы tbank_live/tbank_sandbox. CRITICAL-защиты (из ресёрча по реальному
счёту): идемпотентность order_id с ДИСКРИМИНАТОРОМ ОПЕРАЦИИ (entry/take/unwind — иначе два
логических ордера в одну секунду коллизируют), pre-trade ценовая sanity-проверка, аудит-лог
каждого ордера, атомарность пары с unwind, гейт реальной торговли через armed_cb.

ВАЖНО: боевой post_order тратит реальные деньги. Гейт на УРОВНЕ ОРДЕРА (этот класс):
mode==tbank_real И armed_cb() (real_trading_armed + cooldown 600с). Применяется ко ВСЕМ
ордерам — вход, выход, частичная фиксация, unwind, flat.

`trading_enabled` НЕ проверяется здесь — это гейт ВХОДА на уровне портфеля (St5Portfolio.
can_open, service.py): он блокирует только ОТКРЫТИЕ новой позиции. Выходы/flat/усыновление
исполняются независимо от trading_enabled (сознательно — иначе при trading_enabled=False
открытая позиция залипла бы, её нельзя было бы закрыть). На sandbox — те же методы
SandboxService (armed_cb игнорируется, real=False).
"""
from __future__ import annotations

import hashlib
import time
import uuid

from ..st4 import tbank_sandbox as _sb

_NS = uuid.UUID("5f5e1000-0000-4000-8000-000000000000")   # namespace для детерминированных UUID5


class St5ExecError(Exception):
    pass


def _disc_order_id(account_id: str, uid: str, lots: int, direction: str, op: str, seq: int,
                   real: bool = False) -> str:
    """Идемпотентный orderId с дискриминатором операции и порядковым номером.

    op ∈ {entry, take50, take_rest, unwind, flat}. seq — счётчик в рамках операции. Решает
    коллизию st4-make_order_id (два логических ордера в одну секунду совпадали по id).

    ВАЖНО: SandboxService требует orderId в формате UUID (боевой OrdersService — любую строку).
    Поэтому sandbox → детерминированный UUID5 (валиден + идемпотентен), боевой → sha256-хеш.
    """
    raw = f"{account_id}|{uid}|{int(lots)}|{direction}|{op}|{seq}|{int(time.time())}"
    if real:
        return hashlib.sha256(raw.encode()).hexdigest()[:32]
    return str(uuid.uuid5(_NS, raw))   # sandbox: валидный UUID


class St5PairExecutor:
    """Исполнитель одной пары ST5. real=True — боевой контур; иначе sandbox."""

    def __init__(self, account_id: str, ord_ticker: str, pref_ticker: str,
                 real: bool = False, armed_cb=None, max_price_dev_pct: float = 0.05,
                 audit_cb=None, uid_ord: str | None = None, uid_pref: str | None = None):
        self.account_id = account_id
        self.ord_ticker = ord_ticker
        self.pref_ticker = pref_ticker
        self.real = real
        self.armed_cb = armed_cb                    # () -> bool, взвод реальной торговли
        self.max_price_dev_pct = max_price_dev_pct  # pre-trade: |market−ref|/ref > X% → отказ
        self.audit_cb = audit_cb                    # callback(dict) для аудит-лога каждого ордера
        # uid инструментов: предпочтительно передать ГОТОВЫЕ (резолвленные по коду СЕРИИ, не asset),
        # иначе _uids() резолвит по ticker через find_future (asset-код типа TATN там не находится!)
        self._uid_ord: str | None = uid_ord
        self._uid_pref: str | None = uid_pref
        self._seq = 0
        self._tick_cache: dict[str, float] = {}   # uid -> шаг цены (для лимит-потолка)

    # ---------- ленивый резолв UID инструментов ----------
    def _uids(self) -> tuple[str, str]:
        if self._uid_ord is None:
            self._uid_ord = _sb.find_future(self.ord_ticker)["uid"]
            self._uid_pref = _sb.find_future(self.pref_ticker)["uid"]
        return self._uid_ord, self._uid_pref

    def _tick(self, uid: str) -> float | None:
        """Шаг цены инструмента по UID (для кратности лимит-цены). None — не узнали.
        БАГ до 07.07: резолвил через find_future(asset-код 'SNGR'), а в справочнике коды
        СЕРИЙ 'SNU6' → исключение → tick None → ВСЕ ордера маркетом (лимитки не работали).
        Теперь future_by_uid(uid) — резолв по тому же uid, что и ордера."""
        if uid not in self._tick_cache:
            try:
                it = _sb.future_by_uid(uid)
                self._tick_cache[uid] = _sb._q_to_float(it.get("minPriceIncrement"))
            except Exception:  # noqa: BLE001
                return None
        return self._tick_cache.get(uid) or None

    def _limit_cap(self, uid: str, is_buy: bool) -> float | None:
        """Потолок marketable-limit из стакана: buy → ask+2 тика, sell → bid−2 тика.
        Исполняется мгновенно как маркет, но НЕ глубже потолка (защита от съедания
        тонкого стакана — главная скрытая издержка, см. разбор 02.07). None → маркет."""
        try:
            ob = _sb.order_book(uid, 1)
            lvl = (ob.get("asks") or [None])[0] if is_buy else (ob.get("bids") or [None])[0]
            if not lvl:
                return None
            px = float(lvl[0] if isinstance(lvl, (list, tuple)) else lvl.get("price"))
            tick = self._tick(uid)
            if not tick or px <= 0:
                return None
            cap = px + 2 * tick if is_buy else px - 2 * tick
            return round(round(cap / tick) * tick, 10)   # кратность шагу цены
        except Exception:  # noqa: BLE001  стакан недоступен → маркет (гарантия исполнения)
            return None

    def _post(self, uid: str, lots: int, direction: str, op: str, ref_price: float,
              limit_cap: float | None = None) -> dict:
        """Один боевой/sandbox ордер с защитами. direction: BUY|SELL.
        limit_cap — потолок цены (marketable-limit); None — рыночный."""
        from ..st4 import tbank_live as _live
        # гейт реальной торговли
        if self.real and (self.armed_cb is None or not self.armed_cb()):
            raise St5ExecError("реальная торговля не взведена (armed_cb)")
        # pre-trade ценовая sanity-проверка против last_price
        try:
            mkt = _sb.last_price(uid)
            if mkt > 0 and ref_price > 0 and abs(mkt - ref_price) / ref_price > self.max_price_dev_pct:
                raise St5ExecError(f"аномальная цена {uid}: market={mkt} ref={ref_price} "
                                   f"(>{self.max_price_dev_pct*100:.0f}%)")
        except St5ExecError:
            raise
        except Exception:  # noqa: BLE001  last_price недоступен — не блокируем (market всё равно исполнится)
            mkt = ref_price
        self._seq += 1
        oid = _disc_order_id(self.account_id, uid, lots, direction, op, self._seq, real=self.real)
        full_dir = f"ORDER_DIRECTION_{direction}"
        audit = {"ts": int(time.time() * 1000), "account": self.account_id, "uid": uid,
                 "lots": lots, "direction": direction, "op": op, "order_id": oid,
                 "ref_price": ref_price, "market_price": mkt, "real": self.real,
                 "limit_cap": limit_cap}
        otype = "ORDER_TYPE_LIMIT" if limit_cap else "ORDER_TYPE_MARKET"
        price = _sb.price_q(limit_cap) if limit_cap else None
        try:
            if self.real:
                resp = _live.post_order(self.account_id, uid, lots, full_dir, oid,
                                        order_type=otype, price=price)
            else:
                resp = _sb.post_order(self.account_id, uid, lots, full_dir, oid,
                                      order_type=otype, price=price)
            audit["status"] = "ok"
            audit["executed_lots"] = resp.get("lotsExecuted") or resp.get("executedLots")
        except Exception as e:  # noqa: BLE001
            audit["status"] = "error"
            audit["error"] = str(e)
            if self.audit_cb:
                self.audit_cb(audit)
            raise
        if self.audit_cb:
            self.audit_cb(audit)
        return resp

    def _cancel_rest(self, resp: dict) -> None:
        """Снять недолитый ЛИМИТНЫЙ ордер из стакана — иначе он исполнится позже
        неучтённой ногой. Ошибка отмены не критична (истечёт сам), но логируется."""
        oid = resp.get("orderId")
        if not oid:
            return
        try:
            if self.real:
                from ..st4 import tbank_live as _live
                _live.cancel_order(self.account_id, oid)
            else:
                _sb.cancel_order(self.account_id, oid)
        except Exception as e:  # noqa: BLE001
            if self.audit_cb:
                self.audit_cb({"ts": int(time.time() * 1000), "op": "cancel",
                               "direction": "-", "lots": 0, "uid": oid,
                               "status": f"ошибка отмены: {str(e)[:80]}"})

    @staticmethod
    def _filled(resp: dict, requested: int) -> int:
        """Фактически исполненные лоты из ответа брокера. Нет поля (paper/старый формат) →
        считаем полный филл (совместимость): маркет-ордер без ответа о лотах не проверить."""
        # ВАЖНО: не «or» — lotsExecuted=0 (ничего не налилось, лимитник висит) это ФАКТ 0,
        # а не отсутствие поля (иначе 0-филл прочитается как полный — баг пойман тестом)
        v = resp.get("lotsExecuted")
        if v is None:
            v = resp.get("executedLots")
        if v is None:
            return requested
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return requested

    def open_pair(self, long_spread: bool, lots_ord: int, lots_pref: int,
                  ref_ord: float, ref_pref: float) -> dict:
        """Открыть пару атомарно. long_spread: buy pref + sell ord; иначе наоборот.
        Лоты ног РАЗНЫЕ (β-сайзинг: lots_ord/lots_pref ≈ β).

        Менее ликвидную ногу (pref) первой; при отказе/частичном филле любой ноги —
        unwind РЕАЛЬНО налитого (сверка executed_lots, а не «поверили запросу»).
        """
        uid_ord, uid_pref = self._uids()
        pref_dir = "BUY" if long_spread else "SELL"
        ord_dir = "SELL" if long_spread else "BUY"
        un_pref = "SELL" if pref_dir == "BUY" else "BUY"
        un_ord = "SELL" if ord_dir == "BUY" else "BUY"
        # ВХОДЫ — marketable-limit (потолок ask/bid±2 тика): мгновенное исполнение как
        # маркет, но без съедания тонкого стакана. Выходы/unwind — всегда МАРКЕТ (гарантия).
        cap_pref = self._limit_cap(uid_pref, pref_dir == "BUY")
        cap_ord = self._limit_cap(uid_ord, ord_dir == "BUY")
        # 1) первая нога — pref (менее ликвидная)
        r1 = self._post(uid_pref, lots_pref, pref_dir, "entry", ref_pref, limit_cap=cap_pref)
        got_pref = self._filled(r1, lots_pref)
        if got_pref != lots_pref:
            # частичный филл первой ноги: снять висящий лимитник, откатить налитое
            if cap_pref:
                self._cancel_rest(r1)
            try:
                if got_pref > 0:
                    self._post(uid_pref, got_pref, un_pref, "unwind", ref_pref)
            except Exception as ue:  # noqa: BLE001
                raise St5ExecError(f"частичный филл префа {got_pref}/{lots_pref} И unwind "
                                   f"не удался: {ue}") from ue
            raise St5ExecError(f"частичный филл префа {got_pref}/{lots_pref} — "
                               f"вход отменён, налитое откачено")
        # 2) вторая нога — ord; при отказе/частичном филле откатываем всё налитое
        r2 = None
        try:
            r2 = self._post(uid_ord, lots_ord, ord_dir, "entry", ref_ord, limit_cap=cap_ord)
            got_ord = self._filled(r2, lots_ord)
        except Exception as e:  # noqa: BLE001
            try:
                self._post(uid_pref, lots_pref, un_pref, "unwind", ref_pref)
            except Exception as ue:  # noqa: BLE001
                raise St5ExecError(f"вход сорван И unwind не удался: {e} / {ue}") from e
            raise St5ExecError(f"вторая нога не залилась, первая откачена: {e}") from e
        if got_ord != lots_ord:
            if cap_ord and r2 is not None:
                self._cancel_rest(r2)   # снять висящий лимитник обычки
            errs = []
            try:
                if got_ord > 0:
                    self._post(uid_ord, got_ord, un_ord, "unwind", ref_ord)
            except Exception as ue:  # noqa: BLE001
                errs.append(f"обычка: {ue}")
            try:
                self._post(uid_pref, lots_pref, un_pref, "unwind", ref_pref)
            except Exception as ue:  # noqa: BLE001
                errs.append(f"преф: {ue}")
            if errs:
                raise St5ExecError(f"частичный филл обычки {got_ord}/{lots_ord} И unwind "
                                   f"не удался: {'; '.join(errs)}")
            raise St5ExecError(f"частичный филл обычки {got_ord}/{lots_ord} — "
                               f"вход откачен целиком")
        return {"ok": True}

    def close_pair(self, long_spread: bool, lots_ord: int, lots_pref: int,
                   ref_ord: float, ref_pref: float, op: str = "flat") -> dict:
        """Закрыть пару (полностью или частично). Обратные стороны входа, лоты ног разные.

        Сверка executed_lots: недолив закрывающего ордера НЕ откатываем (закрытие = снижение
        риска, обратный ордер его снова поднял бы) — поднимаем ошибку, caller халтит пару,
        остаток на счёте ловит периодический reconcile."""
        uid_ord, uid_pref = self._uids()
        pref_dir = "SELL" if long_spread else "BUY"   # закрытие префа
        ord_dir = "BUY" if long_spread else "SELL"
        got_pref = self._filled(self._post(uid_pref, lots_pref, pref_dir, op, ref_pref), lots_pref)
        got_ord = self._filled(self._post(uid_ord, lots_ord, ord_dir, op, ref_ord), lots_ord)
        if got_pref != lots_pref or got_ord != lots_ord:
            raise St5ExecError(f"закрытие недолилось: преф {got_pref}/{lots_pref}, "
                               f"обычка {got_ord}/{lots_ord} — остаток на счёте, нужен разбор")
        return {"ok": True}

    # ---------- reconciliation: реальные лоты ног на счёте ----------
    def broker_lots(self) -> tuple[int, int]:
        """(лоты обычки, лоты префа) на счёте, со знаком (+long/−short). Для сверки с движком."""
        from ..st4 import tbank_live as _live
        uid_ord, uid_pref = self._uids()
        src = _live if self.real else _sb
        try:
            pos = src.positions(self.account_id)
        except Exception:  # noqa: BLE001
            return 0, 0
        lots = {}
        for fut in pos.get("futures", []):
            uid = fut.get("instrumentUid") or fut.get("figi")
            bal = int(float(fut.get("balance", 0)))
            lots[uid] = bal
        return lots.get(uid_ord, 0), lots.get(uid_pref, 0)

    def entry_prices(self) -> tuple[float, float]:
        """(ord_entry, pref_entry) — средние цены входа ног (averagePositionPrice из портфеля).
        Для усыновления позиции со счёта при рестарте. 0.0 если ноги нет в портфеле."""
        from ..st4 import tbank_live as _live
        from ..st4.tbank_sandbox import _q_to_float
        uid_ord, uid_pref = self._uids()
        src = _live if self.real else _sb
        try:
            pf = src.portfolio(self.account_id)
        except Exception:  # noqa: BLE001
            return 0.0, 0.0
        prices = {}
        for p in pf.get("positions", []):
            uid = p.get("instrumentUid") or p.get("figi")
            prices[uid] = _q_to_float(p.get("averagePositionPrice"))
        return prices.get(uid_ord, 0.0), prices.get(uid_pref, 0.0)

    def broker_entry_ts(self) -> int | None:
        """Реальное время (unix ms) последней сделки по любой ноге — для усыновления с
        корректным entry_ts (а не моментом рестарта). None если недоступно (caller сделает
        fallback на last_live_ts)."""
        from ..st4 import tbank_live as _live
        src = _live if self.real else _sb
        if not hasattr(src, "last_entry_ts_for"):
            return None
        uid_ord, uid_pref = self._uids()
        best = None
        for uid in (uid_ord, uid_pref):
            try:
                ts = src.last_entry_ts_for(self.account_id, uid)
            except Exception:  # noqa: BLE001
                ts = None
            if ts and (best is None or ts > best):
                best = ts
        return best
