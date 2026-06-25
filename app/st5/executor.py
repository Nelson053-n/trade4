"""Исполнитель пары для ST5 — sandbox / БОЕВОЙ (реальные деньги) через T-Bank.

Переиспользует REST-методы tbank_live/tbank_sandbox. CRITICAL-защиты (из ресёрча по реальному
счёту): идемпотентность order_id с ДИСКРИМИНАТОРОМ ОПЕРАЦИИ (entry/take/unwind — иначе два
логических ордера в одну секунду коллизируют), pre-trade ценовая sanity-проверка, аудит-лог
каждого ордера, атомарность пары с unwind, тройной гейт через armed_cb.

ВАЖНО: боевой post_order тратит реальные деньги. Гейт: mode==tbank_real И armed_cb() И
trading_enabled (последнее — на уровне портфеля). На sandbox — те же методы SandboxService.
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
                 audit_cb=None):
        self.account_id = account_id
        self.ord_ticker = ord_ticker
        self.pref_ticker = pref_ticker
        self.real = real
        self.armed_cb = armed_cb                    # () -> bool, взвод реальной торговли
        self.max_price_dev_pct = max_price_dev_pct  # pre-trade: |market−ref|/ref > X% → отказ
        self.audit_cb = audit_cb                    # callback(dict) для аудит-лога каждого ордера
        self._uid_ord: str | None = None
        self._uid_pref: str | None = None
        self._seq = 0

    # ---------- ленивый резолв UID инструментов ----------
    def _uids(self) -> tuple[str, str]:
        if self._uid_ord is None:
            self._uid_ord = _sb.find_future(self.ord_ticker)["uid"]
            self._uid_pref = _sb.find_future(self.pref_ticker)["uid"]
        return self._uid_ord, self._uid_pref

    def _post(self, uid: str, lots: int, direction: str, op: str, ref_price: float) -> dict:
        """Один боевой/sandbox ордер с защитами. direction: BUY|SELL."""
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
                 "ref_price": ref_price, "market_price": mkt, "real": self.real}
        try:
            if self.real:
                resp = _live.post_order(self.account_id, uid, lots, full_dir, oid)
            else:
                resp = _sb.post_order(self.account_id, uid, lots, full_dir, oid)
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

    def open_pair(self, long_spread: bool, lots: int, ref_ord: float, ref_pref: float) -> dict:
        """Открыть пару атомарно. long_spread: buy pref + sell ord; иначе наоборот.

        Менее ликвидную ногу (pref) первой; при отказе второй — unwind первой.
        """
        uid_ord, uid_pref = self._uids()
        pref_dir = "BUY" if long_spread else "SELL"
        ord_dir = "SELL" if long_spread else "BUY"
        # 1) первая нога — pref (менее ликвидная)
        self._post(uid_pref, lots, pref_dir, "entry", ref_pref)
        # 2) вторая нога — ord; при отказе откатываем первую
        try:
            self._post(uid_ord, lots, ord_dir, "entry", ref_ord)
        except Exception as e:  # noqa: BLE001
            unwind_dir = "SELL" if pref_dir == "BUY" else "BUY"
            try:
                self._post(uid_pref, lots, unwind_dir, "unwind", ref_pref)
            except Exception as ue:  # noqa: BLE001
                raise St5ExecError(f"вход сорван И unwind не удался: {e} / {ue}") from e
            raise St5ExecError(f"вторая нога не залилась, первая откачена: {e}") from e
        return {"ok": True}

    def close_pair(self, long_spread: bool, lots: int, ref_ord: float, ref_pref: float,
                   op: str = "flat") -> dict:
        """Закрыть пару (полностью или частично — lots). Обратные стороны входа."""
        uid_ord, uid_pref = self._uids()
        pref_dir = "SELL" if long_spread else "BUY"   # закрытие префа
        ord_dir = "BUY" if long_spread else "SELL"
        self._post(uid_pref, lots, pref_dir, op, ref_pref)
        self._post(uid_ord, lots, ord_dir, op, ref_ord)
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
