"""St9Session — сервис «трендовой корзины»: 60м бары перпов с ISS, Donchian+ATR движки,
исполнение paper/sandbox (переиспользует tbank_sandbox), учёт по кэшу счёта.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import St9Config
from .engine import St9Engine, Bar, St9Position

ISS = "https://iss.moex.com/iss"
EVENTS_LEN = 60


def _iss(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "trade4-st9"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def _now_ms_frame() -> int:
    """«Сейчас» в той же шкале, что и ts баров: naive-МСК → local timestamp
    (fromisoformat(begin).timestamp() интерпретирует naive как local — искажение
    одинаковое с обеих сторон, сравнение корректно)."""
    now = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=3)
    return int(now.timestamp() * 1000)


def bar_is_closed(begin_ts_ms: int, interval_min: int, now_ms: int) -> bool:
    """Бар закрыт, когда истёк его ПЕРИОД (begin + interval). Фильтровать по полю end
    нельзя: ISS пишет туда время ПОСЛЕДНЕЙ СДЕЛКИ, а не конец периода — формирующийся
    бар всегда проходил проверку `end > now` и потреблялся как закрытый (ревизия 11.07:
    60м бары ели первыми ~10 минутами часа, дневной GAZR — частичным баром дня)."""
    return begin_ts_ms + interval_min * 60_000 <= now_ms


def iss_candles(secid: str, frm: str, interval_min: int = 60) -> list[Bar]:
    """ЗАКРЫТЫЕ свечи фьючерса с ISS (формирующийся бар отброшен). 60м или дневные."""
    iss_iv = 24 if interval_min >= 1440 else interval_min   # ISS: 24 = дневной
    try:
        d = _iss(f"{ISS}/engines/futures/markets/forts/securities/{secid}/candles.json"
                 f"?iss.meta=off&interval={iss_iv}&from={frm}")
        ci = {c: i for i, c in enumerate(d["candles"]["columns"])}
        now_ms = _now_ms_frame()
        out = []
        for r in d["candles"]["data"]:
            ts = int(datetime.fromisoformat(r[ci["begin"]]).timestamp() * 1000)
            if not bar_is_closed(ts, interval_min, now_ms):
                continue
            out.append(Bar(ts=ts, o=float(r[ci["open"]]), h=float(r[ci["high"]]),
                           l=float(r[ci["low"]]), c=float(r[ci["close"]])))
        return out
    except Exception:  # noqa: BLE001
        return []


class St9Session:
    # ГО перечитываем раз в час: биржа меняет ставки редко, но в волатильность —
    # именно тогда, когда устаревшее значение опаснее всего (см. _go_per_lot)
    _GO_CACHE_TTL_SEC = 3600.0
    # запас над лучшей встречной ценой для marketable-limit (в шагах цены).
    # 0 = лимит ровно по встречной (риск недолива на дрожании), больше = ближе к маркету.
    _LIMIT_SLACK_TICKS = 2

    def __init__(self):
        self.cfg = St9Config()
        self.engines: dict[str, St9Engine] = {}
        self.trades: list[dict] = []
        self.events: list[dict] = []
        self.state = {"live": False, "live_intent": False}
        self._session_file = Path(__file__).resolve().parent.parent.parent / "session_state_9.json"
        self._last_bar_ts: dict[str, int] = {}
        self._saved_bar_ts: dict[str, int] = {}       # маркер баров на момент последнего save (персист по тикам)
        self._pv_cache: dict[str, float] = {}
        self._pv_warned: set[str] = set()             # анти-спам warn'ов «pv недоступен»
        self._contract_cache: dict[str, tuple] = {}   # asset -> (secid, дата резолва)
        self._bars_contract: dict[str, str] = {}      # asset -> контракт, чьи бары в движке
        self._pending_positions: dict[str, dict] = {}  # позиции session, ждущие движка (pv)
        self._deferred_ts: dict[str, int] = {}         # secid -> ts отложенного бара (анти-спам лога)
        self.contracts: dict[str, str] = {}           # asset -> контракт ОТКРЫТОЙ позиции (персист)
        self.axis_overrides: dict[str, dict] = {}      # secid -> {don_enter,don_exit,atr_mult,notional} (персист поверх кода)
        self.capital_rub: float = 0.0
        self.exec_anchor: dict | None = None
        self.last_tick_ts: int = 0                    # мс; наблюдаемость живости цикла
        self._hb_ts: float = 0.0                      # heartbeat-событие раз в час
        self._task = None
        self._live_hb = 0.0                            # monotonic последнего успешного прохода run_live (watchdog)
        self._watchdog_stale_min = 25                  # порог зависания цикла, мин (60м бары — редкие проходы)
        self._capital_peak = 0.0                       # пик капитала для стопа просадки (плечо)
        self._dd_halted = False                        # сработал стоп просадки капитала → блок входов
        self.capital_sizing_rub = 0.0                  # ЧЕСТНЫЙ капитал (money+ГО) для сайзинга плеча
        self._go_lot_cache: dict[tuple, tuple] = {}    # (sec,side) -> (ГО на лот, ts кэша)
        self._tick_cache: dict[str, float] = {}        # uid -> шаг цены (справочник тяжёлый)
        self._dd_breach_count = 0                      # подтверждение просадки (2 тика подряд, против битого чтения)
        self._last_fill_px: float | None = None        # средняя цена ПОСЛЕДНЕГО филла (наблюдаемость проскальзывания)
        self._entry_fill_px: dict[str, float] = {}     # secid -> цена филла ВХОДА (персист, живёт до закрытия)
        # RLock: tick (в потоке через to_thread) и мутирующие HTTP-эндпоинты (flat_all/
        # update_axis/update_strategy) не должны пересекаться на движках/позициях (аудит #5:
        # гонка → двойное закрытие). Reentrant — guard внутри tick зовёт flat_all.
        self._lock = threading.RLock()

    def log_event(self, kind: str, message: str) -> None:
        self.events.append({"ts": int(time.time() * 1000), "kind": kind, "message": message})
        if len(self.events) > EVENTS_LEN:
            del self.events[0]
        # дубль в stdout → /var/log/trade4.log: events живут ТОЛЬКО в памяти процесса,
        # без этого разбор инцидента возможен лишь через живой /st9/state (после
        # рестарта история потеряна, а grep по логу давал ложный «движок мёртв»)
        print(f"[st9] {datetime.now().strftime('%H:%M:%S')} {kind}: {message}", flush=True)

    def _pv(self, secid: str) -> float | None:
        """Пункт-стоимость ₽. None при сбое ISS: торговать с неизвестным pv НЕЛЬЗЯ —
        прежний fallback 1.0 давал сайзинг ×1000 на USDRUBF (1250 лотов вместо 1)."""
        if secid not in self._pv_cache:
            try:
                from ..st6.data import point_value
                self._pv_cache[secid] = float(point_value(secid))
            except Exception:  # noqa: BLE001
                return None
        return self._pv_cache[secid]

    def _resolve_contract(self, icfg) -> str | None:
        """Текущий торгуемый контракт актива: ближайший квартальник с экспирацией
        позже чем сегодня + roll_days_before (кэш на день)."""
        from datetime import date as _d
        today = _d.today()
        key = icfg.secid
        c = self._contract_cache.get(key)
        if c and c[1] == today.isoformat():
            return c[0]
        from ..st8.service import _iss_futures_for_asset
        futs = _iss_futures_for_asset(icfg.secid)
        min_exp = (today + timedelta(days=icfg.roll_days_before)).isoformat()
        pick = next((sec for sec, exp in futs if exp > min_exp), None)
        self._contract_cache[key] = (pick, today.isoformat())
        return pick

    def _engine(self, icfg) -> St9Engine | None:
        """Движок оси. None = pv недоступен (сбой ISS) — НЕ создаём с неверным pv,
        ретрай следующим тиком (движок кэширует pv на всю жизнь)."""
        if icfg.secid not in self.engines:
            # квартальник: pv берём с ТЕКУЩЕГО КОНТРАКТА — secid оси (GAZR) это код
            # актива, спецификации у него нет (point_value падал, до 11.07 молча 1.0)
            pv = self._pv(self._trade_secid(icfg) if icfg.quarterly else icfg.secid)
            if pv is None:
                if icfg.secid not in self._pv_warned:
                    self._pv_warned.add(icfg.secid)
                    self.log_event("warn", f"{icfg.secid}: pv недоступен (ISS) — ось на паузе")
                return None
            self._pv_warned.discard(icfg.secid)
            s = self.cfg.strategy
            self.engines[icfg.secid] = St9Engine(
                icfg.secid, icfg.don_enter, icfg.don_exit, icfg.atr_mult,
                s.atr_period, pv=pv,
                fee_per_lot=s.fee_per_lot, allow_short=s.allow_short,
                fee_pct_notional=s.fee_pct_notional)
        return self.engines[icfg.secid]

    # ---------- боевой контур: взвод реальной торговли (канон st5) ----------
    def arm_real(self, armed: bool) -> None:
        """Двойной включатель. Взвод НЕ персистится (сбрасывается рестартом/сменой режима)."""
        self.state["real_trading_armed"] = bool(armed)
        self.log_event("warn" if armed else "info",
                       "🔴 ST9: реальная торговля ВЗВЕДЕНА" if armed else "ST9: взвод снят")

    def _real_armed(self) -> bool:
        """Взвод + cooldown 600с после старта live (защита от автоордеров на всплеске
        сразу после рестарта — сигналы с восстановленных индикаторов)."""
        if not self.state.get("real_trading_armed"):
            return False
        started = self.state.get("session_started") or 0
        return (time.time() - started) >= 600

    def _tick_size(self, uid: str) -> float | None:
        """Шаг цены инструмента по UID. Кэш обязателен: future_by_uid тянет ВЕСЬ
        справочник фьючерсов (~5 МБ), звать его на каждый ордер нельзя.
        ⚠️ Резолв строго по uid, а не по asset-коду: в справочнике лежат коды СЕРИЙ —
        на этом сгорели лимитки st5 (find_future('SNGR') кидал исключение → tick=None →
        все ордера уходили маркетом, а «лимитный режим» полгода был фикцией)."""
        if uid in self._tick_cache:
            return self._tick_cache[uid] or None
        try:
            from ..st4 import tbank_sandbox as sb
            it = sb.future_by_uid(uid)
            self._tick_cache[uid] = sb._q_to_float(it.get("minPriceIncrement"))
        except Exception:  # noqa: BLE001
            return None
        return self._tick_cache.get(uid) or None

    def _limit_cap(self, uid: str, is_buy: bool, lots: int) -> float | None:
        """Потолок marketable-limit из стакана. Это НЕ пассивная лимитка: цена ставится
        ЗА лучшей встречной (buy → выше ask), поэтому ордер исполняется сразу как
        рыночный, но НЕ глубже потолка — защита от съедания стакана в плохой момент.
        Для трендового пробоя это принципиально: пассивная лимитка на входе просто не
        исполнится (цена уходит от нас), и сигнал будет потерян.

        Потолок берём не от первого уровня, а от уровня, где НАБИРАЕТСЯ наш объём,
        плюс запас _LIMIT_SLACK_TICKS. Иначе на тонком стакане лимит отсечёт хвост
        заявки и мы получим частичный филл там, где маркет налил бы полностью.
        None → маркет (стакан недоступен / шаг цены неизвестен — гарантия исполнения
        важнее экономии: пропущенный выход опаснее лишнего тика проскальзывания)."""
        try:
            from ..st4 import tbank_sandbox as sb
            tick = self._tick_size(uid)
            if not tick or tick <= 0:
                return None
            ob = sb.order_book(uid, 10)
            side = ob.get("asks") if is_buy else ob.get("bids")
            if not side:
                return None
            # уровень, на котором накопится нужный объём
            acc, px = 0, None
            for lvl in side:
                acc += int(lvl.get("qty") or 0)
                px = float(lvl.get("price") or 0)
                if acc >= lots:
                    break
            if not px or px <= 0:
                return None
            slack = self._LIMIT_SLACK_TICKS * tick
            cap = px + slack if is_buy else px - slack
            if cap <= 0:
                return None
            return round(round(cap / tick) * tick, 10)   # кратность шагу цены обязательна
        except Exception:  # noqa: BLE001
            return None

    # ---------- исполнение (перп, один инструмент — атомарность не нужна) ----------
    def _cancel_rest(self, resp: dict, real: bool) -> None:
        """Снять недолитый ЛИМИТНЫЙ ордер из стакана (канон st5, executor.py:153).

        Без этого остаток висит в стакане до конца дня и наливается позже, когда цена
        возвращается к потолку: на счёте становится БОЛЬШЕ лотов, чем ведёт движок.
        Лишние — голая позиция без трейла, выход её не закроет (шлёт ордер только на
        известный объём), и расхождение накапливается со следующими сделками.

        Ошибка отмены не критична (ордер истечёт сам к концу сессии), но её надо
        видеть: незамеченный висящий лимитник — источник рассинхрона."""
        oid = resp.get("orderId")
        if not oid:
            return
        try:
            if real:
                from ..st4 import tbank_live as _live
                _live.cancel_order(self.cfg.account_id, oid)
            else:
                from ..st4 import tbank_sandbox as _sb
                _sb.cancel_order(self.cfg.account_id, oid)
        except Exception as e:  # noqa: BLE001
            self.log_event("warn", f"не снят остаток лимитки {str(oid)[:12]}: "
                                   f"{str(e)[:60]}")

    def _order(self, secid: str, lots: int, direction: str, ref_px: float = 0.0) -> int:
        """Market-ордера по 1 лоту (ёмкость). Возвращает ФАКТИЧЕСКИ исполненные лоты:
        отказ в середине серии не должен оставлять слепые лоты (движок ≠ счёт).

        ⚠️ tbank_real: гейт на КАЖДЫЙ ордер (вход/выход/ролл) — armed+cooldown; идемпотентный
        orderId (ретрай не задвоит); sanity цены против ref_px (сигнальный бар)."""
        # СБРОС ДО ВСЕХ return: _last_fill_px общий на оси, tick() обходит их по очереди.
        # Без сброса цена филла оси A залипала бы в сделке оси B на любом раннем выходе
        # (paper, боевой гейт, sanity) — журнал показывал бы чужое проскальзывание.
        self._last_fill_px = None
        if self.cfg.mode not in ("tbank_sandbox", "tbank_real") or not self.cfg.account_id:
            return lots                    # paper: полный виртуальный филл
        real = self.cfg.mode == "tbank_real"
        import hashlib as _hl
        import uuid as _uuid
        from ..st4 import tbank_sandbox as sb
        from ..st4 import tbank_live as live
        uid = sb.find_future(secid)["uid"]
        if real:
            if not self._real_armed():
                self.log_event("warn", f"{secid}: боевой ордер заблокирован — "
                                       f"реальная торговля не взведена/cooldown")
                return 0
            try:   # pre-trade sanity: рынок не должен аномально уехать от сигнальной цены
                mkt = sb.last_price(uid)
                if mkt > 0 and ref_px > 0 and abs(mkt - ref_px) / ref_px > 0.05:
                    self.log_event("warn", f"{secid}: аномальная цена market={mkt} "
                                           f"ref={ref_px} (>5%) — ордер отменён")
                    return 0
            except Exception:  # noqa: BLE001  last_price недоступен — не блокируем
                pass
        filled = 0
        amount_sum = 0.0        # Σ executedOrderPrice по слайсам — для средней цены филла
        priced_lots = 0         # лоты, по которым брокер вернул цену (не все могут)
        # executedOrderPrice — СУММА В РУБЛЯХ за контракт, а НЕ котировка: чтобы получить
        # цену в пунктах, делим на basicAssetSize (IMOEXF=10, USDRUBF=1000, GLDRUBF=1).
        # Без этого филл IMOEXF выглядел как 22146 против цены бара 2214 (замер 28.07).
        try:
            bas = sb._q_to_float(sb.find_future(secid).get("basicAssetSize")) or 1.0
        except Exception:  # noqa: BLE001
            bas = 1.0
        # MARKETABLE-LIMIT одним ордером на весь объём (30.07). Было: N ордеров по 1 лоту —
        # 17 лотов = 17 HTTP round-trip'ов (~1.1с), каждый бьёт по стакану отдельно и цена
        # успевает уйти. Один ордер + потолок цены убирают и лишнюю сеть, и хвост стакана.
        # Потолок НЕ ставим на выходах-по-остатку: там важнее гарантия исполнения.
        use_limit = getattr(self.cfg.strategy, "use_limit_orders", True)
        cap = self._limit_cap(uid, direction == "BUY", lots) if use_limit else None
        slices = [lots] if lots > 0 else []
        last_resp: dict = {}          # ответ последней лимитки — для отмены остатка
        for i, want in enumerate(slices):
            try:
                otype = "ORDER_TYPE_LIMIT" if cap else "ORDER_TYPE_MARKET"
                price = sb.price_q(cap) if cap else None
                if real:
                    raw = (f"{self.cfg.account_id}|{uid}|{want}|{direction}|{i}|"
                           f"{int(time.time())}")
                    oid = _hl.sha256(raw.encode()).hexdigest()[:32]   # идемпотентный
                    resp = live.post_order(self.cfg.account_id, uid, want,
                                           f"ORDER_DIRECTION_{direction}", oid,
                                           order_type=otype, price=price)
                else:
                    resp = sb.post_order(self.cfg.account_id, uid, want,
                                         f"ORDER_DIRECTION_{direction}", str(_uuid.uuid4()),
                                         order_type=otype, price=price)
            except Exception as e:  # noqa: BLE001
                self.log_event("warn", f"{secid}: ордер {direction} прерван "
                                       f"на {filled}/{lots}: {str(e)[:60]}")
                break
            last_resp = resp if isinstance(resp, dict) else {}
            v = resp.get("lotsExecuted")
            if v is None:
                v = resp.get("executedLots")
            try:
                # ⚠️ поле отсутствует → считаем налитым ВЕСЬ объём слайса (канон проекта,
                # st4/tinkoff_executor.py:108). Для лимитки это опаснее, чем для маркета:
                # она МОЖЕТ не исполниться. Поэтому ниже — сверка и добор маркетом.
                got_i = int(float(v)) if v is not None else want
            except (TypeError, ValueError):
                got_i = want
            filled += got_i
            # executedOrderPrice — рублёвая сумма ЗА ОДИН КОНТРАКТ (px×basicAssetSize),
            # БЕЗ множителя лотов: замер 10.08 на проде (USDRUBF 2 лота, bas=1000) дал
            # 40.38 при цене бара 80.61 — ровно px/лоты. Взвешиваем по лотам сами.
            try:
                amt = sb._q_to_float(resp.get("executedOrderPrice"))
                if amt and got_i > 0:
                    amount_sum += amt * got_i
                    priced_lots += got_i
            except Exception:  # noqa: BLE001  цена филла не критична — только наблюдаемость
                pass
        # ДОБОР МАРКЕТОМ: лимитка по своей природе может налить не всё (цена ушла за
        # потолок). Оставлять недолив нельзя — движок считает позицию открытой на
        # запрошенный объём, а на счёте меньше = рассинхрон. Добираем ОДИН раз, без
        # потолка: гарантия исполнения важнее экономии на хвосте.
        if cap and filled < lots:
            rest = lots - filled
            # СНАЧАЛА снять остаток лимитки из стакана, ПОТОМ добирать (канон st5,
            # executor.py:153). Иначе висящий ордер нальётся позже, когда цена вернётся
            # к потолку, и на счёте окажется БОЛЬШЕ лотов, чем ведёт движок: лишние —
            # голая направленная позиция без трейла, которую некому закрыть (выход шлёт
            # ордер лишь на известный движку объём). Порядок важен: отмена до добора,
            # иначе между ними остаётся то же окно.
            self._cancel_rest(last_resp, real)
            self.log_event("info", f"{secid}: лимит налил {filled}/{lots} — "
                                   f"добор {rest} маркетом")
            try:
                if real:
                    raw = (f"{self.cfg.account_id}|{uid}|{rest}|{direction}|top|"
                           f"{int(time.time())}")
                    oid = _hl.sha256(raw.encode()).hexdigest()[:32]
                    resp = live.post_order(self.cfg.account_id, uid, rest,
                                           f"ORDER_DIRECTION_{direction}", oid)
                else:
                    resp = sb.post_order(self.cfg.account_id, uid, rest,
                                         f"ORDER_DIRECTION_{direction}", str(_uuid.uuid4()))
                v = resp.get("lotsExecuted") or resp.get("executedLots")
                got_i = int(float(v)) if v is not None else rest
                filled += got_i
                amt = sb._q_to_float(resp.get("executedOrderPrice"))
                if amt and got_i > 0:
                    amount_sum += amt * got_i
                    priced_lots += got_i
            except Exception as e:  # noqa: BLE001
                self.log_event("warn", f"{secid}: добор маркетом не удался: {str(e)[:60]}")
        # amount_sum взвешен по лотам выше → /priced_lots даёт сумму за КОНТРАКТ,
        # /bas приводит её к КОТИРОВКЕ, в которой движок считает P&L.
        self._last_fill_px = (amount_sum / priced_lots / bas) if priced_lots > 0 else None
        return filled

    def _trade_secid(self, icfg) -> str:
        """Что реально торгуем: перп = secid; квартальник = текущий контракт."""
        return (self._resolve_contract(icfg) or icfg.secid) if icfg.quarterly else icfg.secid

    def _daily_loss_hit(self) -> float | None:
        """Достигнут ли дневной лимит убытка. Возвращает P&L дня (₽) при пробое, иначе None.

        Аудит 12.08: параметр объявлен в конфиге и меняется через API, но в st9 НЕ ЧИТАЛСЯ
        НИ РАЗУ — оператор, выставивший лимит, получал подтверждение и нулевую защиту.
        Это опаснее отсутствия ручки. Реализация по канону st7 (service.py:186).

        Гейтит ТОЛЬКО ВХОД: выходы/flat/ролл от лимита не зависят, иначе при пробое
        позиция залипла бы (закрыть нельзя) — тот же принцип, что у trading_enabled.

        День — по МСК (торговая сессия FORTS). ⚠️ exit_ts сделок пишется через
        time.time() — это НАСТОЯЩИЙ epoch, а не сдвинутая шкала баров `_now_ms_frame`;
        смешивать их нельзя, поэтому день считается от UTC+3 по обеим сторонам."""
        lim = getattr(self.cfg.strategy, "daily_loss_limit_rub", 0.0)
        if not lim or lim <= 0:
            return None

        def _msk_day(ms: float) -> str:
            return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(
                timezone(timedelta(hours=3))).strftime("%Y-%m-%d")

        today = _msk_day(time.time() * 1000)
        day_net = sum(t.get("net_pnl_rub", 0) for t in self.trades
                      if t.get("exit_ts") and _msk_day(t["exit_ts"]) == today)
        return day_net if day_net < -abs(lim) else None

    def _entry_lots(self, icfg, px: float, pv: float, side: str = "long",
                    sec: str | None = None, atr: float | None = None) -> int:
        """Лоты входа. Три режима, в порядке приоритета:

        1. РИСК-САЙЗИНГ (`risk_per_trade_rub>0`, пер-ось): размер от бюджета риска,
           `лоты = risk / (atr_mult × ATR × pv)` — знаменатель это цена стопа в ₽.
           Включён точечно на USDRUBF (замер 12.08: +49.5% при равной просадке,
           5 лет из 5); на IMOEXF режим ВРЕДЕН (−67.5%), поэтому не глобальный.
        2. ПЛЕЧО (`go_target_pct>0`): из фактического ГО на лот.
        3. НОТИОНАЛ оси (по умолчанию).

        В tbank_real режется потолком real_max_notional_rub (пилот).
        side/sec нужны для точного ГО при плече; atr — для риск-сайзинга."""
        sec = sec or self._trade_secid(icfg)
        if px <= 0 or pv <= 0:
            # битая цена (ISS отдал 0/None) → ОТКАЗ входа, НЕ 1 лот вслепую: при px=0
            # ref_px=0 отключал бы sanity-гейт в _order (условие ref_px>0) → вход по
            # мусорной цене с непредсказуемым нотионалом. 0 лотов = вход не состоится.
            self.log_event("warn", f"{icfg.secid}: вход отклонён — битая цена px={px} pv={pv}")
            return 0
        s = self.cfg.strategy
        target = icfg.entry_notional_rub
        # ── РЕЖИМ 1: САЙЗИНГ ПО РИСКУ (пер-ось). Знаменатель — цена стопа в ₽ на лот:
        # позиция закрывается по трейлу примерно в atr_mult×ATR от входа.
        risk = getattr(icfg, "risk_per_trade_rub", 0.0)
        if risk > 0:
            if not atr or atr <= 0:
                # ATR не прогрет — на риск-оси размер посчитать НЕЧЕМ. Падать на нотионал
                # нельзя: он для этой оси не откалиброван и дал бы чужой размер вслепую.
                # Молча: этот путь зовётся и для предварительного lots_for_entry на КАЖДОМ
                # баре прогрева — лог бы захлебнулся. Реальный вход всё равно не состоится
                # (0 лотов), а сигнала при непрогретом ATR движок и не даст.
                return 0
            risk_per_lot = icfg.atr_mult * atr * pv
            if risk_per_lot <= 0:
                return 0
            lots = max(1, int(risk / risk_per_lot))
            cap = getattr(s, "real_max_notional_rub", 0.0)
            if self.cfg.mode == "tbank_real" and cap > 0:
                lots = min(lots, max(1, int(cap / (px * pv))))
            return lots
        # режим утилизации капитала: нотионал от % капитала на число осей (плечо)
        go_pct = getattr(s, "go_target_pct", 0.0)
        # ЧЕСТНЫЙ капитал (free+ГО), НЕ totalAmountPortfolio (искажён переоценкой шорта)
        cap_base = self.capital_sizing_rub or self.capital_rub
        if go_pct > 0 and cap_base > 0:
            # ДЕЛИТЕЛЬ = ВЕСЬ РЕЕСТР осей, не len(self.engines) (аудит 30.07, HIGH).
            # Движки создаются ЛЕНИВО, по одному, внутри того же цикла tick(), который
            # сайзит входы: ось №1 делила на 1, №2 на 2, №3 на 3 → на ПЕРВОМ тике после
            # каждого рестарта суммарное ГО выходило ×1.8 бюджета, а первая ось забирала
            # его целиком (20 лотов USDRUBF при бюджете на 3 оси). Позиции держатся
            # днями — перекос переживал рестарт. Реестр — величина постоянная, от
            # порядка обхода и момента рестарта не зависит.
            # ...но выведенные из состава оси (entries_enabled=False) в делитель НЕ идут:
            # они больше не входят, а место в бюджете занимали бы — ровно та цена простоя
            # (≈3.5 п.п. годовых), из-за которой 30.07 выключали GAZR.
            n_axes = max(1, sum(1 for i in self.cfg.instruments if i.entries_enabled))
            go_per_axis = cap_base * (go_pct / 100.0) / n_axes   # целевое ГО на ось
            # лоты ПРЯМО из ФАКТИЧЕСКОГО ГО на лот (GetFuturesMargin), НЕ через
            # захардкоженный go_frac 0.044 — аудит 15.07: реальное ГО USDRUBF ≈0.15
            # нотионала, не 0.044 → go_frac завышал плечо ~в 3×, а в стресс ГО растёт.
            go_lot = self._go_per_lot(sec, side)
            if go_lot and go_lot > 0:
                lots = max(1, int(go_per_axis / go_lot))
                cap = getattr(s, "real_max_notional_rub", 0.0)
                if self.cfg.mode == "tbank_real" and cap > 0:
                    lots = min(lots, max(1, int(cap / (px * pv))))
                return lots
            # ГО оси недоступно (сбой API) — фолбэк на go_frac-оценку (лучше чем ничего)
            go_frac = getattr(s, "go_frac", 0.044) or 0.044
            target = go_per_axis / go_frac
        cap = getattr(s, "real_max_notional_rub", 0.0)
        if self.cfg.mode == "tbank_real" and cap > 0:
            target = min(target, cap)     # боевой потолок (пилот) поверх любого сайзинга
        return max(1, int(target / (px * pv)))

    def _go_per_lot(self, sec: str, side: str) -> float | None:
        """Фактическое ГО на 1 лот контракта (брокерский GetFuturesMargin), сторона важна.
        Кэш на (sec, side) с TTL — прогрев зовёт сотни раз/бар, но вечный кэш опасен:
        биржа поднимает ставки В ВОЛАТИЛЬНОСТЬ, ровно когда мы в просадке, а устаревшее
        (заниженное) ГО дало бы двойной размер именно в стресс (аудит 30.07, MED).
        Невалидное ГО (<=0 из-за пустого ответа API) НЕ кэшируется: иначе одно битое
        чтение навсегда роняло сайзинг в фолбэк go_frac=0.044, а это ×3.4 к плечу."""
        key = (sec, side)
        cached = self._go_lot_cache.get(key)
        if cached is not None and time.time() - cached[1] < self._GO_CACHE_TTL_SEC:
            return cached[0]
        try:
            from ..st4 import tbank_sandbox as sb
            uid = sb.find_future(sec)["uid"]
            mlong, mshort = sb.futures_margin(uid)
            val = mlong if side == "long" else mshort
            if not val or val <= 0:
                raise ValueError(f"ГО<=0 ({val})")
            self._go_lot_cache[key] = (val, time.time())
            return val
        except Exception:  # noqa: BLE001
            # протухший кэш лучше, чем фолбэк go_frac (он завышает плечо ~3×)
            return cached[0] if cached is not None else None

    @staticmethod
    def _slip_rub(tr, entry_fill: float | None, exit_fill: float | None,
                  pv: float) -> float | None:
        """Проскальзывание сделки в ₽: насколько ФАКТИЧЕСКИЕ филлы хуже цен бара, по
        которым движок посчитал P&L. Отрицательное = исполнились хуже модели (норма).
        None, если брокер не вернул цену хотя бы одной ноги — врать нулём нельзя.

        ГЕЙТ ПРАВДОПОДОБИЯ (инцидент 30.07): филл, расходящийся с ценой бара >20%, —
        не проскальзывание, а другая ЕДИНИЦА (рублёвая сумма вместо котировки: филл
        USDRUBF 79310 при цене 79.06 дал фиктивные +739010₽). Такие записи считаем
        неизмеренными. Опасны именно ПОЛОЖИТЕЛЬНЫЕ — «выигрыш на исполнении» может
        сойти за основание увеличить размер позиции."""
        if entry_fill is None or exit_fill is None or not pv:
            return None
        for fill, bar_px in ((entry_fill, tr.entry), (exit_fill, tr.exit)):
            if not bar_px or abs(fill - bar_px) / abs(bar_px) > 0.20:
                return None
        d = 1 if tr.side == "long" else -1
        model = (tr.exit - tr.entry) * d
        fact = (exit_fill - entry_fill) * d
        return round((fact - model) * tr.lots * pv, 2)

    def _apply_signal(self, eng: St9Engine, sig: dict, icfg) -> None:
        with self._lock:                # против гонки с flat_all/update_* из HTTP (аудит #5)
            self._apply_signal_locked(eng, sig, icfg)

    def _apply_signal_locked(self, eng: St9Engine, sig: dict, icfg) -> None:
        ts = int(time.time() * 1000)
        sec = self._trade_secid(icfg)
        try:
            if sig["act"] in ("close", "reverse"):
                closing = eng.position
                # закрываем на контракте, где позиция ОТКРЫВАЛАСЬ (не на свежем)
                close_sec = self.contracts.get(icfg.secid, sec) if icfg.quarterly else sec
                direction = "SELL" if closing.side == "long" else "BUY"
                got = self._order(close_sec, closing.lots, direction, ref_px=sig["px"])
                if got < closing.lots:      # одна повторная попытка добить остаток
                    got += self._order(close_sec, closing.lots - got, direction,
                                       ref_px=sig["px"])
                if got < closing.lots:
                    # ЧАСТИЧНОЕ ЗАКРЫТИЕ: движок ведёт ОСТАТОК (трейл продолжает защищать),
                    # а закрытая часть фиксируется как сделка — иначе её P&L терялся бы
                    # навсегда, а комиссия входа за полный объём висела на остатке
                    # (аудит 10.08, HIGH-3). Выход по остатку повторится, когда step()
                    # снова даст сигнал — трейл его гарантирует.
                    if got > 0:
                        ptr = eng.close_partial(sig["px"], got, ts, sig["reason"] + "_partial")
                        self.trades.append(dict(ptr.__dict__))
                    self.log_event("warn", f"🚨 {eng.secid}: закрыто {got} лотов, остаток "
                                           f"{closing.lots} — выход повторится следующим баром")
                    self.save_session()
                    return
                exit_fill = self._last_fill_px
                entry_fill = self._entry_fill_px.pop(icfg.secid, None)
                tr = eng.close(sig["px"], ts, sig["reason"])
                # НАБЛЮДАЕМОСТЬ ИЗДЕРЖЕК: журнал ведёт P&L по цене БАРА (sig["px"]) — это
                # сознательно, менять экономику сделок здесь нельзя. Рядом кладём цены
                # ФАКТИЧЕСКИХ филлов и проскальзывание в ₽: без них издержки исполнения
                # неизмеримы (история операций sandbox через REST недоступна — 404).
                rec = dict(tr.__dict__)
                rec["entry_fill"] = entry_fill
                rec["exit_fill"] = exit_fill
                rec["slip_rub"] = self._slip_rub(tr, entry_fill, exit_fill, eng.pv)
                self.trades.append(rec)
                self.contracts.pop(icfg.secid, None)
                slip_s = (f", проскальзывание {rec['slip_rub']:+.0f}₽"
                          if rec["slip_rub"] is not None else "")
                self.log_event("exit", f"{eng.secid}: выход {tr.side} ({tr.reason}) "
                                       f"net {tr.net_pnl_rub:+.0f}₽{slip_s}")
            # ВЫХОД выше уже исполнен — гейт стоит ТОЛЬКО на открытии. Ось с
            # entries_enabled=False доживает открытую позицию под трейлом и больше
            # не входит (вывод из состава корзины без голых лотов на счёте).
            if (sig["act"] in ("open", "reverse") and self.cfg.trading_enabled
                    and icfg.entries_enabled):
                day_net = self._daily_loss_hit()
                if day_net is not None:
                    self.log_event("warn", f"🚨 {eng.secid}: дневной лимит убытка "
                                           f"{self.cfg.strategy.daily_loss_limit_rub:.0f}₽ "
                                           f"достигнут (день {day_net:+.0f}₽) — вход отменён")
                    self.save_session()
                    return
                side = sig["new_side"]
                if icfg.quarterly:
                    pv = self._pv(sec)       # pv контракта (может отличаться между сериями)
                    if pv is None:
                        self.log_event("warn", f"{eng.secid}: pv {sec} недоступен — вход пропущен")
                        self.save_session()
                        return
                    eng.pv = pv
                lots = self._entry_lots(icfg, sig["px"], eng.pv, side, sec,
                                        atr=sig.get("atr"))
                got = self._order(sec, lots, "BUY" if side == "long" else "SELL",
                                  ref_px=sig["px"])
                if got <= 0:
                    self.log_event("warn", f"{eng.secid}: вход не исполнен (0 лотов налито)")
                else:
                    eng.open(side, sig["px"], got, ts, sig["atr"])
                    # цена филла входа живёт до закрытия сделки (переживает рестарт).
                    # Запись БЕЗУСЛОВНАЯ: при неизвестной цене ключ надо СТЕРЕТЬ, иначе
                    # значение прошлой сделки приклеится к новой и сфабрикует
                    # «выигрыш на исполнении» (положительный slip → ложный повод расти).
                    if self._last_fill_px:
                        self._entry_fill_px[icfg.secid] = self._last_fill_px
                    else:
                        self._entry_fill_px.pop(icfg.secid, None)
                    if icfg.quarterly:
                        self.contracts[icfg.secid] = sec
                    fill_s = (f" (филл {self._last_fill_px:.4g})"
                              if self._last_fill_px else "")
                    self.log_event("position", f"{eng.secid}: {side.upper()} {got}лот"
                                               f"{' '+sec if sec!=eng.secid else ''} @ {sig['px']}"
                                               f"{fill_s}")
            self.save_session()
        except Exception as e:  # noqa: BLE001
            # ПЕРСИСТ ОБЯЗАТЕЛЕН И НА ОШИБКЕ (аудит 30.07, HIGH): между eng.close() и
            # save_session() состояние движка уже изменено (позиция снята, сделка в
            # журнале). Исключение посреди последовательности (сбой find_future на
            # реверсе, atr=None) оставляло session-файл со СТАРОЙ позицией: после
            # рестарта движок усыновлял позицию, которой на счёте нет, и первым же
            # «выходом» слал реальный ордер — голая нога. Сделка при этом терялась.
            self.log_event("warn", f"{eng.secid}: исполнение не удалось: {str(e)[:80]}")
            try:
                self.save_session()
            except Exception:  # noqa: BLE001
                self.log_event("warn", f"{eng.secid}: 🚨 состояние НЕ сохранено после сбоя")

    def _roll(self, eng: St9Engine, icfg, old_sec: str, new_sec: str) -> None:
        """Ролл квартальника: закрыть трейд на старом контракте (reason=roll),
        переоткрыть ту же сторону на новом по его цене. Бары движка чистятся
        (индикаторы бэкфиллятся новым контрактом на следующем тике)."""
        ts = int(time.time() * 1000)
        try:
            p = eng.position
            new_pv = self._pv(new_sec)     # pv ДО закрытия старого: нет pv — ролл откладываем
            if new_pv is None:
                self.log_event("warn", f"{eng.secid}: ролл отложен — pv {new_sec} недоступен")
                return
            old_q = iss_candles(old_sec, (datetime.now(timezone.utc)
                                          - timedelta(days=5)).strftime("%Y-%m-%d"),
                                icfg.interval_min)
            new_q = iss_candles(new_sec, (datetime.now(timezone.utc)
                                          - timedelta(days=5)).strftime("%Y-%m-%d"),
                                icfg.interval_min)
            old_px = old_q[-1].c if old_q else p.entry
            new_px = new_q[-1].c if new_q else old_px
            side = p.side
            # трейл переносим отступом от ТЕКУЩЕЙ цены (не от entry: у прибыльной позиции
            # трейл давно подтянут к цене, отступ от entry резко ослаблял защиту)
            trail_off_pct = abs(old_px - p.trail) / old_px if old_px else 0.03
            direction = "SELL" if side == "long" else "BUY"
            got = self._order(old_sec, p.lots, direction, ref_px=old_px)
            if got < p.lots:
                got += self._order(old_sec, p.lots - got, direction, ref_px=old_px)
            if got < p.lots:
                # закрытую часть фиксируем сделкой (аудит 10.08, HIGH-3) — иначе её P&L
                # теряется, а входная комиссия за полный объём остаётся на остатке
                if got > 0:
                    ptr = eng.close_partial(old_px, got, ts, "roll_partial")
                    self.trades.append(dict(ptr.__dict__))
                self.log_event("warn", f"🚨 {eng.secid}: ролл прерван — закрыто {got}, "
                                       f"остаток {p.lots} на {old_sec}, повтор следующим тиком")
                self.save_session()
                return
            exit_fill = self._last_fill_px
            entry_fill = self._entry_fill_px.pop(icfg.secid, None)
            tr = eng.close(old_px, ts, "roll")
            rec = dict(tr.__dict__)
            rec["entry_fill"] = entry_fill
            rec["exit_fill"] = exit_fill
            rec["slip_rub"] = self._slip_rub(tr, entry_fill, exit_fill, eng.pv)
            self.trades.append(rec)
            eng.pv = new_pv
            lots = self._entry_lots(icfg, new_px, new_pv, side, new_sec,
                                    atr=eng._atr())
            got2 = self._order(new_sec, lots, "BUY" if side == "long" else "SELL",
                               ref_px=new_px)
            if got2 <= 0:
                self.contracts.pop(icfg.secid, None)
                eng.bars.clear()
                self.log_event("warn", f"🚨 {eng.secid}: ролл — {new_sec} не налился, "
                                       f"старый закрыт, остаёмся flat")
                self.save_session()
                return
            atr_equiv = new_px * trail_off_pct / eng.atr_mult
            eng.open(side, new_px, got2, ts, atr_equiv)
            if self._last_fill_px:      # филл НОВОГО контракта — база для след. сделки
                self._entry_fill_px[icfg.secid] = self._last_fill_px
            else:                       # цена неизвестна — стереть, не тащить старую
                self._entry_fill_px.pop(icfg.secid, None)
            # бары чистим, last_bar_ts НЕ трогаем: следующий тик увидит «last>0, баров нет»
            # и сделает БЭКФИЛЛ индикаторов без сигналов. Прежний pop() уводил в ветку
            # «первого прогрева», которая стирала position — реальные лоты оставались
            # на счёте бесхозными (критический баг, ревизия 11.07)
            eng.bars.clear()
            self._bars_contract[icfg.secid] = new_sec
            self.contracts[icfg.secid] = new_sec
            self.log_event("info", f"{eng.secid}: РОЛЛ {old_sec}→{new_sec} "
                                   f"{side} {got2}лот @ {new_px} (net старого {tr.net_pnl_rub:+.0f}₽)")
            self.save_session()
        except Exception as e:  # noqa: BLE001
            # тот же инвариант, что в _apply_signal_locked: старый контракт мог быть уже
            # закрыт (eng.close выполнен), а исключение прилетело на открытии нового —
            # без персиста session хранил бы позицию на СТАРОМ, уже проданном контракте
            self.log_event("warn", f"{eng.secid}: ролл не удался: {str(e)[:80]}")
            try:
                self.save_session()
            except Exception:  # noqa: BLE001
                self.log_event("warn", f"{eng.secid}: 🚨 состояние НЕ сохранено после сбоя ролла")

    # ---------- тик ----------
    def _forts_open(self) -> bool:
        """Торги FORTS идут сейчас. Гейт исполнения: дневной бар GAZR «закрывается» в 00:00
        (биржа закрыта с 23:50) — без гейта каждый его сигнал улетал в закрытую биржу
        (HTTP 400, инцидент 18.07), а бар съедался безвозвратно. При сбое расписания —
        fail-open (торгуем как раньше), чтобы не заморозить выходы."""
        try:
            from ..st5 import forts_schedule as sched
            minute, _sec, dow = sched.msk_minute_dow()
            return sched.forts_kind(minute, dow) == "live"
        except Exception:  # noqa: BLE001
            return True

    def tick(self) -> dict:
        acted = {"signals": 0}
        market_open = self._forts_open()
        self._try_restore_positions()
        for icfg in self.cfg.instruments:
            eng = self._engine(icfg)
            if eng is None:
                continue   # pv недоступен (сбой ISS) — ось на паузе, ретрай следующим тиком
            # инструмент котировок: перп = сам secid; квартальник = текущий контракт
            trade_sec = icfg.secid
            if icfg.quarterly:
                fresh_c = self._resolve_contract(icfg)
                if not fresh_c:
                    continue
                held_c = self.contracts.get(icfg.secid)
                # ролл шлёт ордера — при закрытой бирже откладываем (ретрай тиком после открытия)
                if eng.position is not None and held_c and held_c != fresh_c and market_open:
                    self._roll(eng, icfg, held_c, fresh_c)
                trade_sec = fresh_c
                if eng.position is not None and not held_c:
                    self.contracts[icfg.secid] = fresh_c
                # смена котируемого контракта ВО ФЛЭТЕ: бары старой серии в окне Donchian
                # дают ложный «пробой» на базисе → чистим, бэкфилл соберёт новую серию
                if (eng.position is None and eng.bars
                        and self._bars_contract.get(icfg.secid) not in (None, fresh_c)):
                    eng.bars.clear()
                self._bars_contract[icfg.secid] = fresh_c
            # горизонт истории: 60м — 14 дней; дневки — 90 (окна 20д + ATR прогрев)
            hist_days = 90 if icfg.interval_min >= 1440 else 14
            last0 = self._last_bar_ts.get(icfg.secid, 0)
            need_backfill = last0 > 0 and not eng.bars
            frm = (datetime.fromtimestamp(last0 / 1000).strftime("%Y-%m-%d")
                   if last0 and not need_backfill
                   else (datetime.now(timezone.utc) - timedelta(days=hist_days)).strftime("%Y-%m-%d"))
            bars = iss_candles(trade_sec, frm, icfg.interval_min)
            last = last0
            if need_backfill:
                # рестарт: восстановить состояние индикаторов УЖЕ ОБРАБОТАННЫМИ барами
                # (без step — без сигналов/сделок), иначе входы заблокированы 2-3 дня прогрева
                hist = [b for b in bars if b.ts <= last]
                for b in hist:
                    eng.bars.append(b)
                if hist:
                    self.log_event("info", f"{icfg.secid}: индикаторы восстановлены "
                                           f"({len(hist)} баров после рестарта)")
            fresh = [b for b in bars if b.ts > last]
            warmup = last == 0 and eng.position is None   # первый запуск: только прогрев,
            # аномалия: позиция есть, а маркер баров потерян — историю доливаем без сигналов,
            # живым считаем только последний бар (трейл в step защитит позицию)
            if last == 0 and eng.position is not None and len(fresh) > 1:
                for b in fresh[:-1]:
                    eng.bars.append(b)
                    self._last_bar_ts[icfg.secid] = b.ts
                fresh = fresh[-1:]
            for b in fresh:       # warmup: БЕЗ сделок (иначе журнал засоряют входы истории)
                if not warmup and not market_open:
                    # биржа закрыта/клиринг: бар НЕ съедаем (маркер не двигаем) — сигнал
                    # родится первым тиком после открытия и ордер пройдёт. Иначе вход
                    # терялся навсегда: got=0, а бар уже обработан (инцидент GAZR 18.07)
                    if self._deferred_ts.get(icfg.secid) != b.ts:
                        self._deferred_ts[icfg.secid] = b.ts
                        self.log_event("info", f"{icfg.secid}: бар отложен до открытия FORTS")
                    break
                self._last_bar_ts[icfg.secid] = b.ts
                # предварительный размер для step(); фактический пересчитывается в
                # _apply_signal_locked с верной стороной. На риск-оси без прогретого ATR
                # вернётся 0 — это нормально, step() значение не использует.
                lots = self._entry_lots(icfg, b.c, eng.pv, "long",
                                        self._trade_secid(icfg), atr=eng._atr())
                sig = eng.step(b, lots_for_entry=lots)
                if sig and not warmup:
                    acted["signals"] += 1
                    self._apply_signal(eng, sig, icfg)
            if warmup and fresh:
                # position и так None (warmup только во флэте) — стирать НЕЛЬЗЯ:
                # прежний безусловный сброс убивал позицию после ролла (ревизия 11.07)
                self.log_event("info", f"{icfg.secid}: прогрет ({len(fresh)} баров), старт flat")
        self.refresh_capital()
        self._capital_dd_guard()          # предохранитель просадки капитала (плечо)
        self.last_tick_ts = int(time.time() * 1000)
        if time.time() - self._hb_ts > 3600:          # heartbeat: тики st9 тихие,
            self._hb_ts = time.time()                 # без него живость не видна
            npos = sum(1 for e in self.engines.values() if e.position)
            self.log_event("info", f"цикл жив: {len(self.cfg.instruments)} осей, позиций {npos}")
        # персист при ПРОДВИЖЕНИИ по барам: save_session зовётся только по событиям
        # (сделка/старт/ролл), поэтому во флэте файл замирал на днях и читался как
        # «движок мёртв» (ложная тревога 03.08). Не каждый тик — только смена маркера.
        if self._last_bar_ts != self._saved_bar_ts:
            self._saved_bar_ts = dict(self._last_bar_ts)
            self.save_session()
        return acted

    def ledger(self, days_back: int = 30) -> dict:
        """Операции счёта (кэш-истина): покупки/продажи ног, комиссии, вариационная маржа.
        ГЛАВНОЕ, чего не показывал журнал движка — оператор «не видел сделки», потому что
        trades пишутся только при ЗАКРЫТИИ, а операции ОТКРЫТИЯ (SELL×N лотов) и varmargin
        живут только на счёте. sandbox — GetSandboxOperations; paper — синтез из журнала."""
        rows: list[dict] = []
        varmargin = 0.0
        if self.cfg.mode in ("tbank_sandbox", "tbank_real") and self.cfg.account_id:
            try:
                from ..st4 import tbank_sandbox as sb
                now = datetime.now(timezone.utc)
                frm = (now - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
                to = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                if self.cfg.mode == "tbank_real":
                    from ..st4 import tbank_live as live
                    ops = live.operations(self.cfg.account_id, frm, to)
                else:
                    ops = sb._call("tinkoff.public.invest.api.contract.v1.SandboxService",
                                   "GetSandboxOperations",
                                   {"accountId": self.cfg.account_id, "from": frm, "to": to,
                                    "state": "OPERATION_STATE_EXECUTED"},
                                   token=sb._account_token(self.cfg.account_id)).get("operations", [])
                for o in ops:
                    amt = sb._q_to_float(o.get("payment"))
                    kind = o.get("operationType", "").replace("OPERATION_TYPE_", "").lower()
                    if kind == "accruing_varmargin":
                        varmargin += amt
                    rows.append({"date": str(o.get("date", ""))[:16].replace("T", " "),
                                 "kind": kind, "qty": o.get("quantity", 0) or 0,
                                 "label": o.get("figi") or "счёт", "amount": round(amt, 2)})
                rows.sort(key=lambda r: r["date"], reverse=True)
            except Exception as e:  # noqa: BLE001
                rows.append({"date": "", "kind": "error", "qty": 0,
                             "label": str(e)[:80], "amount": 0})
        else:
            for t in self.trades:
                lbl = f"{t.get('secid')} {t.get('side')} {t.get('lots')}лот"
                rows.append({"date": "", "kind": "trade_pnl", "qty": t.get("lots", 0),
                             "label": lbl, "amount": round(t.get("gross_pnl_rub", 0), 2)})
                if t.get("fees_rub"):
                    rows.append({"date": "", "kind": "fee", "qty": 0,
                                 "label": f"комиссия {t.get('secid')}",
                                 "amount": -round(t["fees_rub"], 2)})
        free_cash = None
        try:
            if self.cfg.mode in ("tbank_sandbox", "tbank_real") and self.cfg.account_id:
                from ..st4 import tbank_sandbox as sb
                free_cash = round(sb.free_money_rub(self.cfg.account_id))
        except Exception:  # noqa: BLE001
            pass
        net = sum(t.get("net_pnl_rub", 0) for t in self.trades)
        return {"rows": rows[:200], "free_cash_rub": free_cash,
                "varmargin_rub": round(varmargin, 2),
                "journal_net_rub": round(net),
                "fees_total_rub": round(sum(t.get("fees_rub", 0) for t in self.trades), 2)}

    def price_series(self, secid: str, days_back: int = 20) -> dict:
        """Свечи + Donchian-канал + линия ATR-трейла + метка входа для canvas-графика.
        Считает индикаторы тем же движком (no-repaint Donchian, ATR), что и торговля."""
        icfg = next((i for i in self.cfg.instruments if i.secid == secid), None)
        if icfg is None:
            return {"secid": secid, "bars": [], "error": "неизвестная ось"}
        trade_sec = self._trade_secid(icfg)
        # для дневных осей (GAZR) 20 календарных дней = ~14 торговых баров — Donchian(20)
        # не прогревается (канал None). Гарантируем ≥ don_enter+15 торговых баров: переводим
        # в календарные дни через ~1.5× (выходные/праздники). Внутридневные ТФ не трогаем.
        if icfg.interval_min >= 1440:
            need_days = int((icfg.don_enter + 15) * 1.5)
            days_back = max(days_back, need_days)
        frm = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        bars = iss_candles(trade_sec, frm, icfg.interval_min)
        if not bars:
            return {"secid": secid, "trade_secid": trade_sec, "bars": []}
        s = self.cfg.strategy
        n_in, n_out, atr_n = icfg.don_enter, icfg.don_exit, s.atr_period
        out = []
        for k, b in enumerate(bars):
            win_in = bars[max(0, k - n_in):k]          # без текущего (no-repaint)
            win_out = bars[max(0, k - n_out):k]
            don_hi = max((x.h for x in win_in), default=None) if len(win_in) >= n_in else None
            don_lo = min((x.l for x in win_in), default=None) if len(win_in) >= n_in else None
            don_hi_out = max((x.h for x in win_out), default=None) if len(win_out) >= n_out else None
            don_lo_out = min((x.l for x in win_out), default=None) if len(win_out) >= n_out else None
            out.append({"ts": b.ts, "o": b.o, "h": b.h, "l": b.l, "c": b.c,
                        "don_hi": don_hi, "don_lo": don_lo,
                        "don_hi_out": don_hi_out, "don_lo_out": don_lo_out})
        eng = self.engines.get(secid)
        pos = None
        if eng and eng.position:
            p = eng.position
            pos = {"side": p.side, "entry": p.entry, "lots": p.lots,
                   "trail": round(p.trail, 2), "entry_ts": p.entry_ts}
        # закрытые сделки этой оси в окне баров — для отрисовки вход→выход на графике
        t_lo = out[0]["ts"] if out else 0
        deals = [{"side": t.get("side"), "entry": t.get("entry"), "exit": t.get("exit"),
                  "lots": t.get("lots"), "entry_ts": t.get("entry_ts"),
                  "exit_ts": t.get("exit_ts"), "net_pnl_rub": t.get("net_pnl_rub"),
                  "reason": t.get("reason")}
                 for t in self.trades
                 if t.get("secid") == secid and (t.get("exit_ts") or 0) >= t_lo]
        return {"secid": secid, "trade_secid": trade_sec,
                "don": f"{n_in}/{n_out}", "interval_min": icfg.interval_min,
                "bars": out, "position": pos, "deals": deals}

    def flat_all(self) -> dict:
        """Паник-закрытие ВСЕХ открытых осей по рынку (штатно: ордер + журнал + save).
        В отличие от ручного скрипта делает И eng.close() И self.trades.append() — журнал
        не теряет запись. Закрытие на контракте ОТКРЫТИЯ (self.contracts) для квартальников.
        Гейт ордера (armed/cooldown в tbank_real) работает как в tick — flat не в обход."""
        with self._lock:                # против гонки с tick (двойное закрытие, аудит #5)
            return self._flat_all_locked()

    def _flat_all_locked(self) -> dict:
        from ..st4 import tbank_sandbox as sb
        ts = int(time.time() * 1000)
        closed, partial = [], []
        for icfg in self.cfg.instruments:
            eng = self.engines.get(icfg.secid)
            if eng is None or eng.position is None:
                continue
            p = eng.position
            close_sec = self.contracts.get(icfg.secid, self._trade_secid(icfg)) \
                if icfg.quarterly else self._trade_secid(icfg)
            # текущая цена для журнальной записи (нет сигнального бара)
            try:
                px = sb.last_price(sb.find_future(close_sec)["uid"])
            except Exception:  # noqa: BLE001
                px = p.entry     # фолбэк: цена входа (P&L=0, реальный покажет счёт)
            direction = "SELL" if p.side == "long" else "BUY"
            got = self._order(close_sec, p.lots, direction, ref_px=px)
            if got < p.lots:                 # одна повторная попытка добить остаток
                got += self._order(close_sec, p.lots - got, direction, ref_px=px)
            if got <= 0:
                self.log_event("warn", f"{eng.secid}: flat-all не исполнен (0 лотов)")
                continue
            if got < p.lots:                 # частичное: ведём остаток, закрытую часть — в журнал
                ptr = eng.close_partial(px, got, ts, "flat_all_partial")
                self.trades.append(dict(ptr.__dict__))
                partial.append({"secid": eng.secid, "closed": got, "left": p.lots})
                self.log_event("warn", f"🚨 {eng.secid}: flat-all закрыл {got}, остаток {p.lots}")
                continue
            exit_fill = self._last_fill_px
            entry_fill = self._entry_fill_px.pop(icfg.secid, None)
            tr = eng.close(px, ts, "flat_all")
            rec = dict(tr.__dict__)
            rec["entry_fill"] = entry_fill
            rec["exit_fill"] = exit_fill
            rec["slip_rub"] = self._slip_rub(tr, entry_fill, exit_fill, eng.pv)
            self.trades.append(rec)
            self.contracts.pop(icfg.secid, None)
            closed.append({"secid": eng.secid, "side": tr.side, "lots": tr.lots,
                           "exit": tr.exit, "net_pnl_rub": tr.net_pnl_rub})
            self.log_event("exit", f"{eng.secid}: flat-all {tr.side} net {tr.net_pnl_rub:+.0f}₽")
        self.save_session()
        return {"ok": True, "closed": closed, "partial": partial}

    def update_axis(self, secid: str, params: dict) -> dict:
        """Пер-ось настройки: don_enter/don_exit/atr_mult/entry_notional_rub.
        Сигнальные параметры (don/atr) меняются ТОЛЬКО когда ось flat — иначе смена
        трейла/окна на открытой позиции даёт рассинхрон с уже открытой ставкой.
        Нотионал можно менять всегда (влияет лишь на будущий сайзинг)."""
        with self._lock:                # пересоздание движка не должно пересечься с tick
            return self._update_axis_locked(secid, params)

    def _update_axis_locked(self, secid: str, params: dict) -> dict:
        icfg = next((i for i in self.cfg.instruments if i.secid == secid), None)
        if icfg is None:
            raise ValueError(f"неизвестная ось {secid}")
        eng = self.engines.get(secid)
        in_pos = eng is not None and eng.position is not None
        signal_keys = ("don_enter", "don_exit", "atr_mult")
        wants_signal = any(k in params and params[k] is not None for k in signal_keys)
        if wants_signal and in_pos:
            raise ValueError(f"{secid} в позиции — параметры сигналов меняются только на flat")

        def _num(key, lo, hi, cur, cast=float):
            if key not in params or params[key] is None:
                return cur
            v = cast(params[key])
            if not (lo <= v <= hi):
                raise ValueError(f"{key}: вне [{lo}, {hi}]")
            return v

        icfg.don_enter = _num("don_enter", 2, 200, icfg.don_enter, int)
        icfg.don_exit = _num("don_exit", 1, 200, icfg.don_exit, int)
        icfg.atr_mult = _num("atr_mult", 0.5, 10.0, icfg.atr_mult, float)
        icfg.entry_notional_rub = _num("entry_notional_rub", 1000, 100_000_000,
                                       icfg.entry_notional_rub, float)
        # оверрайд для персиста: instruments перечитываются из КОДА при рестарте
        # (реестр осей из кода, ловушка 09.07 с GAZR) — параметры храним отдельно и
        # накладываем поверх кодового реестра при load_session
        self.axis_overrides[secid] = {
            "don_enter": icfg.don_enter, "don_exit": icfg.don_exit,
            "atr_mult": icfg.atr_mult, "entry_notional_rub": icfg.entry_notional_rub}
        # применить к живому движку (если создан): обновить поля + расширить буфер баров
        if eng is not None:
            eng.don_enter = icfg.don_enter
            eng.don_exit = icfg.don_exit
            eng.atr_mult = icfg.atr_mult
            need = max(icfg.don_enter, icfg.don_exit, eng.atr_period) + 2
            if eng.bars.maxlen < need + 60:            # окно выросло — расширить deque
                from collections import deque as _dq
                eng.bars = _dq(eng.bars, maxlen=need + 60)
        self.log_event("info", f"{secid}: параметры обновлены "
                               f"(Donchian {icfg.don_enter}/{icfg.don_exit}, "
                               f"ATR×{icfg.atr_mult}, нотионал {int(icfg.entry_notional_rub)})")
        self.save_session()
        return {"secid": secid, "don_enter": icfg.don_enter, "don_exit": icfg.don_exit,
                "atr_mult": icfg.atr_mult, "entry_notional_rub": icfg.entry_notional_rub,
                "applied_to_engine": eng is not None}

    def _capital_dd_guard(self) -> None:
        """ПРЕДОХРАНИТЕЛЬ при плече: стоп на просадку КАПИТАЛА от пика. Трейл каждой позы
        не спасает от хвостового разворота всего портфеля с плечом — этот guard тормозит
        на уровне счёта. При capital < peak×(1−pct/100): flat всех осей + блок входов
        (trading_enabled=False). Сбрасывается вручную (оператор оценил и перезапустил)."""
        pct = getattr(self.cfg.strategy, "capital_dd_stop_pct", 0.0)
        # честный капитал (money+ГО), не искажённый totalAmountPortfolio
        cap = self.capital_sizing_rub or self.capital_rub
        if pct <= 0 or cap <= 0:
            return
        # защита пика от АНОМАЛЬНОГО ВЫБРОСА (аудит #7): единичный битый cap (сбой API вернул
        # мусор) навсегда подтянул бы пик вверх (max монотонен) → floor завышен → стоп НЕ
        # сработает при реальной просадке. Пик не растёт скачком >15% за тик (капитал при
        # плече 3× физически не может так прыгнуть мгновенно) — аномалию игнорируем.
        if self._capital_peak > 0 and cap > self._capital_peak * 1.15:
            self.log_event("warn", f"капитал скакнул аномально ({cap:.0f} vs пик "
                                   f"{self._capital_peak:.0f}) — пик НЕ обновлён (защита стопа)")
        else:
            self._capital_peak = max(self._capital_peak, cap)
        # ЗАЩЁЛКА СНИМАЕТСЯ ВОЗОБНОВЛЕНИЕМ ВХОДОВ (аудит 30.07, HIGH). Раньше стояло
        # безусловное `if self._dd_halted: return` — и стоп умирал навсегда: оператор
        # возвращал входы штатным /st9/control/trading?on=true (тот путь _dd_halted не
        # трогает), после чего капитал мог падать хоть на 60% — guard молчал, флэта не
        # делал. Т.е. единственная портфельная защита при плече отключалась незаметно.
        # Теперь halt держится, только пока входы реально заблокированы; вернули входы —
        # вернулась и защита (пик уже пересчитан выше, floor поедет от нового пика).
        if self._dd_halted:
            if not self.cfg.trading_enabled:
                return
            self._dd_halted = False
            self._dd_breach_count = 0
            self.log_event("info", "стоп просадки снят: входы возобновлены оператором — "
                                   "предохранитель снова активен")
        floor = self._capital_peak * (1 - pct / 100.0)
        if cap >= floor:
            self._dd_breach_count = 0                  # просадки нет — сброс счётчика
            return
        # ПОДТВЕРЖДЕНИЕ: требуем просадку 2 тика ПОДРЯД, иначе единичное битое чтение cap
        # (сбой API вернул заниженное) ложно закрыло бы все позиции с плечом (аудит #7).
        self._dd_breach_count += 1
        if self._dd_breach_count < 2:
            self.log_event("warn", f"капитал ниже порога ({cap:.0f} < {floor:.0f}) — "
                                   f"ждём подтверждения (тик {self._dd_breach_count}/2)")
            return
        dd = (1 - cap / self._capital_peak) * 100
        self._dd_halted = True
        self.cfg.trading_enabled = False               # блок входов (выходы/flat живут)
        self.log_event("warn", f"🚨 СТОП ПРОСАДКИ КАПИТАЛА: {dd:.1f}% от пика "
                               f"{self._capital_peak:.0f} (порог {pct}%) — flat всех осей, входы СТОП")
        try:
            self.flat_all()
        except Exception as e:  # noqa: BLE001
            self.log_event("warn", f"flat при стопе просадки не удался: {str(e)[:80]}")
        self.save_session()

    def update_strategy(self, params: dict) -> dict:
        """Strategy-level параметры ST9: плечо (go_target_pct), стоп просадки капитала
        (capital_dd_stop_pct) и МОДЕЛЬ ИЗДЕРЖЕК (fee_pct_notional / fee_per_lot).
        ⚠️ БОЕВОЙ РИСК: go_target_pct>0 включает плечо. Устанавливать оба вместе
        (плечо без предохранителя опасно). Инициализирует пик от текущего капитала.

        Комиссия была недоступна ни через API, ни через UI — а session побеждает код,
        и на проде до 14.08 жил `fee_per_lot=2.0` из модели, признанной фиктивной ещё
        30.07 (замер: ровно 0.05% нотионала, 292 сделки). Узнать о расхождении можно
        было только заглянув в session-файл. Те же грабли, что с poll_seconds (48194cc)."""
        s = self.cfg.strategy
        ranges = {"go_target_pct": (0, 50), "capital_dd_stop_pct": (0, 90),
                  "go_frac": (0.005, 0.5),
                  "fee_pct_notional": (0, 1), "fee_per_lot": (0, 50)}
        for key, (lo, hi) in ranges.items():
            if key not in params or params[key] is None:
                continue
            v = float(params[key])
            if not (lo <= v <= hi):
                raise ValueError(f"{key}: вне [{lo}, {hi}]")
            setattr(s, key, v)
        # ПРОБРОС В ЖИВЫЕ ДВИЖКИ: параметры комиссии читаются в St9Engine при СОЗДАНИИ,
        # а движки кэшируются в self.engines — без этого правка молча не подействовала бы
        # до рестарта (и выглядела бы применённой).
        if "fee_pct_notional" in params or "fee_per_lot" in params:
            for eng in self.engines.values():
                eng.fee_pct_notional = s.fee_pct_notional
                eng.fee_per_lot = s.fee_per_lot
            self.log_event("info", f"модель издержек: {s.fee_pct_notional}% нотионала"
                                   f" + {s.fee_per_lot}₽/лот (применено к живым движкам)")
        # при включении плеча/стопа — инициализировать пик от ЧЕСТНОГО капитала сейчас,
        # иначе guard мог бы сработать от нулевого/искажённого пика
        if s.capital_dd_stop_pct > 0:
            cap = self.capital_sizing_rub or self.capital_rub
            if cap > 0 and self._capital_peak <= 0:
                self._capital_peak = cap
        self.log_event("warn" if s.go_target_pct > 0 else "info",
                       f"strategy обновлена: плечо go_target={s.go_target_pct}% "
                       f"стоп_просадки={s.capital_dd_stop_pct}% (пик {self._capital_peak:.0f})")
        self.save_session()
        return {"go_target_pct": s.go_target_pct, "capital_dd_stop_pct": s.capital_dd_stop_pct,
                "go_frac": s.go_frac, "capital_peak_rub": round(self._capital_peak),
                "capital_sizing_rub": round(self.capital_sizing_rub) or None,
                "fee_pct_notional": s.fee_pct_notional, "fee_per_lot": s.fee_per_lot}

    def reset_dd_halt(self) -> dict:
        """Сброс стопа просадки капитала (оператор оценил и решил продолжить). Сбрасывает
        halt + переустанавливает пик на текущий капитал (чтобы не сработал сразу снова).
        Входы включаются отдельно (trading_enabled) — осознанно."""
        self._dd_halted = False
        self._dd_breach_count = 0
        # пик — из ТОЙ ЖЕ серии, что меряет guard (capital_sizing_rub), а не из
        # capital_rub=totalAmountPortfolio (аудит 30.07): тот искажён mark-to-market и
        # завышал пик → эффективный допуск был вдвое уже порога, а в paper/до первого
        # чтения портфеля capital_rub=0 обнулял пик и глушил стоп совсем.
        self._capital_peak = self.capital_sizing_rub or self.capital_rub
        self.log_event("info", f"стоп просадки сброшен (пик → {self._capital_peak:.0f})")
        self.save_session()
        return {"dd_halted": False, "capital_peak_rub": round(self._capital_peak),
                "trading_enabled": self.cfg.trading_enabled}

    def _execution_gap(self) -> float | None:
        """Разница «факт счёта − модельный журнал» с момента якоря (₽).

        Отрицательная = скрытая стоимость исполнения: спред, проскальзывание, филлы мимо
        журнала. Канон проекта «истина = счёт, журналы врут» до 11.08 у ST9 не был
        реализован вовсе — exec_anchor писался и персистился, но НЕ ЧИТАЛСЯ (аудит 10.08,
        MED-1). При размере 400к/ось цена незамеченного расхождения выросла вчетверо.

        База — capital_sizing_rub (free + фактическое ГО), НЕ totalAmountPortfolio:
        последний искажён mark-to-market фьючерсов (завышал ~на 77к) и сам же код в трёх
        местах называет его недостоверным. Сверять модель с искажённой серией бессмысленно.

        ⚠️ ТОЛЬКО ВО ФЛЭТЕ. `free + ГО` НЕ содержит вариационной маржи (ГО — залог, он не
        меняется от хода цены), а модель с unrealized — содержит. Сравнение этих величин
        при открытой позиции даёт «разрыв» ровно размером с вармаржу: замер 11.08 на
        проде показал −23 901₽ при unrealized +24 217₽ — то есть мерил не издержки, а
        плавающую прибыль. Реализованный P&L сверять корректно только когда все позиции
        закрыты и вармаржа осела в деньгах.

        None — нет якоря, не sandbox/real, чужой счёт, капитал не прочитан или есть
        открытые позиции."""
        a = self.exec_anchor
        if a is None or self.cfg.mode not in ("tbank_sandbox", "tbank_real"):
            return None
        if a.get("account_id") != self.cfg.account_id:
            return None
        base = a.get("capital_sizing")
        if not base:            # якорь старого формата (до 11.08) — сверять не с чем
            return None
        cap = self.capital_sizing_rub
        if not cap:
            return None
        if any(e.position is not None for e in self.engines.values()):
            return None         # см. «ТОЛЬКО ВО ФЛЭТЕ» выше
        net = sum(t.get("net_pnl_rub", 0) for t in self.trades)
        model_delta = net - a.get("net", 0.0)
        fact_delta = cap - float(base)
        return round(fact_delta - model_delta)

    def refresh_capital(self) -> None:
        if self.cfg.mode not in ("tbank_sandbox", "tbank_real") or not self.cfg.account_id:
            return
        try:
            from ..st4 import tbank_sandbox as sb
            if self.cfg.mode == "tbank_real":
                from ..st4 import tbank_live as live
                pf = live.portfolio(self.cfg.account_id)
            else:
                pf = sb.portfolio(self.cfg.account_id)
            total = sb._q_to_float(pf.get("totalAmountPortfolio") or pf.get("totalAmountCurrencies"))
            if total and total > 0:
                self.capital_rub = float(total)
            # ЧЕСТНЫЙ капитал для СAЙЗИНГА плеча = свободные деньги + ФАКТИЧЕСКОЕ ГО открытых
            # позиций. НЕ totalAmountPortfolio (искажён mark-to-market фьючерса, завысил бы
            # ~на 77к) и НЕ top-key "blocked" (там валютные блокировки, не фьючерсное ГО —
            # аудит 15.07 нашёл: читалось случайное 4412 вместо ГО). ГО берём точное из
            # брокерского GetFuturesMargin на лот × лоты позиции.
            try:
                free = sb.free_money_rub(self.cfg.account_id)
                go_open = 0.0
                missed = []                  # оси, чьё ГО прочитать НЕ удалось
                for sec, eng in self.engines.items():
                    p = eng.position
                    if p is None:
                        continue
                    try:
                        uid = sb.find_future(self.contracts.get(sec, sec))["uid"]
                        mlong, mshort = sb.futures_margin(uid)
                        go = (mlong if p.side == "long" else mshort) * p.lots
                        if go <= 0:          # 0 = _q_to_float на пустом ответе, не «ГО ноль»
                            raise ValueError("ГО<=0")
                        go_open += go
                    except Exception:  # noqa: BLE001
                        missed.append(sec)
                # НЕ ОБНОВЛЯТЬ капитал, если ГО хоть одной открытой оси неизвестно
                # (аудит 30.07, MED): при плече ГО ≈15% капитала, и потеря пары осей
                # роняет «честный капитал» на треть — стоп просадки видел фиктивную
                # просадку и закрывал ВЕСЬ портфель по рынку, хотя капитал не двигался.
                # Подтверждение «2 тика подряд» тут не спасает: сбой API коррелирован
                # между тиками, а не случаен. Лучше держать прошлое значение капитала
                # (сайзинг чуть устареет), чем сфабриковать просадку.
                if missed:
                    self.log_event("warn", f"ГО недоступно по осям {','.join(missed)} — "
                                           f"капитал не обновлён (защита от ложного стопа)")
                elif free > 0:
                    self.capital_sizing_rub = float(free + go_open)
                    # ЯКОРЬ ставится здесь, а не на totalAmountPortfolio: сверка идёт по
                    # ТОЙ ЖЕ серии, что и база (free+ГО). Разные серии в базе и в замере
                    # давали бы «разрыв» размером с mark-to-market, а не с издержками.
                    # ТОЛЬКО ВО ФЛЭТЕ: при открытой позиции в base попала бы вармаржа,
                    # которой нет в модели, и разрыв врал бы на её величину навсегда.
                    if ((self.exec_anchor is None
                         or not self.exec_anchor.get("capital_sizing"))
                            and not any(e.position is not None
                                        for e in self.engines.values())):
                        self.exec_anchor = {
                            "capital_sizing": self.capital_sizing_rub,
                            "net": sum(t.get("net_pnl_rub", 0) for t in self.trades),
                            "account_id": self.cfg.account_id,
                            "ts": int(time.time() * 1000)}
                        self.save_session()   # иначе якорь живёт только в памяти и
                        # сбрасывается рестартом — накопленная сверка обнулялась бы
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

    async def run_live(self) -> None:
        import asyncio
        self.state["live"] = True
        self._live_hb = time.monotonic()      # старт: ещё не завис
        while self.state["live"]:
            try:
                # wait_for: зависший тик (DNS-фаза вне urllib-timeout) не убивает цикл
                await asyncio.wait_for(asyncio.to_thread(self.tick),
                                       timeout=max(120.0, self.cfg.poll_seconds * 0.9))
                self._live_hb = time.monotonic()   # проход завершён → watchdog спокоен
            except asyncio.TimeoutError:
                self.log_event("warn", "тик завис (таймаут) — пропущен, цикл жив")
            except Exception as e:  # noqa: BLE001
                self.log_event("warn", f"тик не удался: {str(e)[:100]}")
            await asyncio.sleep(self.cfg.poll_seconds)

    def _watchdog_should_restart(self, now_mono: float, ts_sec: float | None = None) -> bool:
        """Завис ли live-цикл. True ⇔ live И биржа открыта (forts live) И с последнего
        успешного прохода прошло > _watchdog_stale_min мин. Биржу проверяем, чтобы НЕ
        рестартовать ночью/в выходные (баров нет легитимно — не зависание)."""
        if not self.state.get("live"):
            return False
        try:
            from ..st5 import forts_schedule as sched
            minute, _sec, dow = sched.msk_minute_dow(ts_sec)
            if sched.forts_kind(minute, dow) != "live":
                return False
        except Exception:  # noqa: BLE001  нет расписания — не блокируем watchdog
            pass
        if self._live_hb <= 0:
            return False
        return (now_mono - self._live_hb) > self._watchdog_stale_min * 60

    async def watchdog_loop(self) -> None:
        """Сторож зависания live-цикла ST9 (канон st5): раз в 60с проверяет, при застое
        отменяет залипшую run_live и поднимает новую. autoresume стартует цикл после
        рестарта сервиса, но НЕ ловит зависание ПОСЛЕ старта — эта дыра для направленной
        позиции с плечом критична (трейл перестаёт двигаться)."""
        import asyncio
        while True:
            await asyncio.sleep(60)
            try:
                if not self._watchdog_should_restart(time.monotonic()):
                    continue
                stale = int((time.monotonic() - self._live_hb) / 60)
                self.log_event("warn", f"watchdog: live-цикл ST9 завис ({stale}м) — перезапуск")
                t = self._task
                if t is not None and not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                self._task = None
                self.start_live()
            except Exception as e:  # noqa: BLE001
                self.log_event("warn", f"watchdog ST9 ошибка: {e}")

    def start_live(self) -> None:
        import asyncio
        if self._task is not None and not self._task.done():
            return   # цикл реально жив (проверка task, НЕ флага — флаг бывает фиктивным)
        self.state["live"] = True
        self.state["live_intent"] = True
        self.state["session_started"] = time.time()   # точка отсчёта cooldown боевого взвода
        self._task = asyncio.create_task(self.run_live())
        self.log_event("info", f"ST9 live запущен ({self.cfg.mode}, трендовая корзина)")
        self.save_session()

    def stop_live(self) -> None:
        self.state["live"] = False
        self.state["live_intent"] = False
        self.log_event("info", "ST9 остановлен")
        self.save_session()

    # ---------- снимок/персист ----------
    def snapshot(self) -> dict:
        net = sum(t.get("net_pnl_rub", 0) for t in self.trades)
        return {
            "strategy": "st9", "live": self.state["live"], "mode": self.cfg.mode,
            "account_id": self.cfg.account_id or None,
            "trading_enabled": self.cfg.trading_enabled,
            "real_trading_armed": bool(self.state.get("real_trading_armed")),  # боевой взвод
            "instruments": [
                {"secid": i.secid, "don": f"{i.don_enter}/{i.don_exit}",
                 "notional_rub": i.entry_notional_rub,
                 "entries_enabled": i.entries_enabled,   # False = ось на выводе
                 "position": (lambda p: {"side": p.side, "entry": p.entry, "lots": p.lots,
                                         "trail": round(p.trail, 2)} if p else None)(
                     self.engines.get(i.secid).position if i.secid in self.engines else None),
                 "last_signal": self.engines[i.secid].last_signal if i.secid in self.engines else ""}
                for i in self.cfg.instruments
            ],
            "net_pnl_rub": round(net),
            "trades_count": len(self.trades),
            "trades_tail": self.trades[-20:],
            "last_tick_ts": self.last_tick_ts,
            "capital_rub": round(self.capital_rub) or None,
            # ЧЕСТНЫЙ капитал (free+ГО) — для KPI дашборда; capital_rub (totalAmountPortfolio)
            # искажён переоценкой шорта фьючерса и пугает оператора мусорной цифрой
            "capital_sizing_rub": round(self.capital_sizing_rub) or None,
            # СВЕРКА С ИСТИНОЙ: «факт счёта − модель журнала» с момента якоря. Отрицательное
            # = журнал рисует больше, чем принёс счёт (скрытые издержки исполнения).
            "execution_gap_rub": self._execution_gap(),
            # дневной лимит убытка: величина и факт срабатывания (0 = выключен)
            "daily_loss_limit_rub": self.cfg.strategy.daily_loss_limit_rub,
            "daily_loss_hit": self._daily_loss_hit(),
            # утилизация капитала / плечо и предохранитель просадки (наблюдаемость)
            "go_target_pct": self.cfg.strategy.go_target_pct,
            "capital_dd_stop_pct": self.cfg.strategy.capital_dd_stop_pct,
            "capital_peak_rub": round(self._capital_peak) or None,
            "dd_halted": self._dd_halted,
            "slippage": self._slippage_summary(),
            "events": self.events[-20:],
        }

    def _drop_implausible_entry_fills(self, positions: dict) -> None:
        """Выбросить цены филлов, не похожие на котировку (>20% от цены входа позиции).
        Файлы до фикса 28.07 хранят РУБЛЁВУЮ СУММУ (IMOEXF: 22146 при цене бара 2214) —
        такая запись дала бы фиктивное проскальзывание в миллионы при закрытии."""
        for sec, pd in positions.items():
            fill = self._entry_fill_px.get(sec)
            entry = (pd or {}).get("entry")
            if fill and entry and abs(fill - entry) / entry > 0.20:
                self._entry_fill_px.pop(sec, None)
                self.log_event("warn", f"{sec}: цена филла входа {fill:.4g} не похожа на "
                                       f"котировку (вход {entry}) — замер сброшен")

    @staticmethod
    def _fills_plausible(t: dict) -> bool:
        """Похожи ли цены филлов записи журнала на котировку (а не на рублёвую сумму).
        Тот же порог 20%, что в _slip_rub/_drop_implausible_entry_fills.
        Записи БЕЗ цен филлов признаём правдоподобными: их slip_rub посчитан корректно
        (сделки до появления полей entry_fill/exit_fill) — проверять нечего."""
        for fk, bk in (("entry_fill", "entry"), ("exit_fill", "exit")):
            fill, bar_px = t.get(fk), t.get(bk)
            if fill is None or not bar_px:
                continue
            if abs(fill - bar_px) / abs(bar_px) > 0.20:
                return False
        return True

    def _slippage_summary(self) -> dict:
        """Сводка ИЗМЕРЕННОГО проскальзывания по закрытым сделкам. Показывает, насколько
        фактические филлы разошлись с ценами бара, по которым движок считает P&L.
        measured < len(trades) — норма: у сделок до внедрения замера цен филлов нет.
        Записи с филлами в другой ЕДИНИЦЕ (рублёвая сумма вместо котировки, сделки до
        фикса 30.07) отбрасываются тем же гейтом 20%, что и в _slip_rub — иначе сводка
        показывала бы фикцию вида avg +191436₽."""
        vals = [t["slip_rub"] for t in self.trades
                if isinstance(t, dict) and t.get("slip_rub") is not None
                and self._fills_plausible(t)]
        if not vals:
            return {"measured": 0, "trades": len(self.trades), "total_rub": None,
                    "avg_rub": None, "worst_rub": None}
        return {"measured": len(vals), "trades": len(self.trades),
                "total_rub": round(sum(vals), 2),
                "avg_rub": round(sum(vals) / len(vals), 2),
                "worst_rub": round(min(vals), 2)}

    def _try_restore_positions(self) -> None:
        """Восстановить позиции из session в движки. Отложенно: если при загрузке pv был
        недоступен (движок не создался), позиция ждёт в _pending_positions до успеха."""
        for sec, pd in list(self._pending_positions.items()):
            icfg = next((i for i in self.cfg.instruments if i.secid == sec), None)
            if icfg is None:
                self._pending_positions.pop(sec)
                continue
            eng = self._engine(icfg)
            if eng is None:
                continue   # pv недоступен — попробуем следующим тиком
            try:
                eng.position = St9Position(**pd)
                self.log_event("info", f"{sec}: позиция восстановлена из session")
            except Exception:  # noqa: BLE001
                self.log_event("warn", f"{sec}: позиция из session не восстановлена")
            self._pending_positions.pop(sec)

    def save_session(self) -> None:
        try:
            # позиции ПЕРСИСТЯТСЯ (грабли st5); pending — ещё не восстановленные (pv ждём),
            # без объединения save до первого тика стирал бы их из файла
            pos = {sec: e.position.__dict__
                   for sec, e in self.engines.items() if e.position}
            for sec, pd in self._pending_positions.items():
                pos.setdefault(sec, pd)
            data = {"config": self.cfg.model_dump(), "trades": self.trades,
                    "state": self.state, "last_bar_ts": self._last_bar_ts,
                    "exec_anchor": self.exec_anchor,
                    "contracts": self.contracts,
                    "axis_overrides": self.axis_overrides,
                    "capital_peak": self._capital_peak, "dd_halted": self._dd_halted,
                    "entry_fill_px": self._entry_fill_px,
                    "positions": pos}
            # АТОМАРНО: пишем во временный файл рядом и подменяем os.replace (аудит 10.08).
            # write_text рвёт файл на середине при OOM (на этом сервере OOM срабатывал) —
            # load_session тогда падает на битом JSON и стартует БЕЗ позиций, тогда как
            # лоты живут на счёте. Замена атомарна в пределах одной ФС, поэтому tmp
            # кладём в тот же каталог.
            tmp = self._session_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False))
            os.replace(tmp, self._session_file)
        except Exception as e:  # noqa: BLE001
            # НЕ глотать: обработчики в _apply_signal рассчитывают увидеть провал персиста
            # (их ветка «🚨 состояние НЕ сохранено» была недостижима, пока save_session
            # возвращался молча). Диск полон/права слетели — оператор должен знать.
            self.log_event("warn", f"🚨 состояние НЕ сохранено: {str(e)[:80]}")

    def load_session(self) -> bool:
        if not self._session_file.exists():
            return False
        try:
            d = json.loads(self._session_file.read_text())
        except Exception:  # noqa: BLE001
            return False
        self.trades = d.get("trades", [])
        self.state.update(d.get("state") or {})
        # live — РАНТАЙМ-факт (жив ли цикл СЕЙЧАС), из файла не восстанавливается:
        # иначе start_live() видит live=True и выходит, НЕ создав task → фиктивный live
        # с мёртвым циклом (баг найден 09.07: st9 жил без цикла после рестарта)
        self.state["live"] = False
        self.state["real_trading_armed"] = False   # взвод НЕ переживает рестарт (safe)
        self._last_bar_ts = {k: int(v) for k, v in (d.get("last_bar_ts") or {}).items()}
        self.exec_anchor = d.get("exec_anchor") or None
        self.contracts = dict(d.get("contracts") or {})
        # цена филла ВХОДА живёт до закрытия сделки — переживает рестарт под позицией,
        # иначе проскальзывание такой сделки посчитать уже нечем (None вместо вранья)
        self._entry_fill_px = {k: float(v) for k, v in
                               (d.get("entry_fill_px") or {}).items() if v}
        self._drop_implausible_entry_fills(d.get("positions") or {})
        cfg = d.get("config")
        if cfg:
            try:
                self.cfg = St9Config(**cfg)
                # РЕЕСТР ИНСТРУМЕНТОВ — ИЗ КОДА, не из session (как ST4_PAIRS): иначе
                # добавленная в код ось затирается старым сохранённым списком
                # (ловушка 09.07: GAZR исчез после рестарта — файл был от v1 с 2 осями)
                self.cfg.instruments = St9Config().instruments
                # poll_seconds — ТОЖЕ ИЗ КОДА (та же ловушка, поймана 30.07): это
                # операционный параметр (лаг исполнения), а не настройка оператора.
                # Смена 600→60с в коде была инертна — session перетирал её старым 600.
                self.cfg.poll_seconds = St9Config().poll_seconds
            except Exception:  # noqa: BLE001
                pass
        # пер-ось оверрайды параметров поверх кодового реестра (переживают рестарт)
        # стоп просадки капитала ПЕРЕЖИВАЕТ рестарт (иначе halt снялся бы молча и входы
        # возобновились в просадке); сбрасывается только явным reset_dd_halt оператором
        self._capital_peak = float(d.get("capital_peak") or 0.0)
        self._dd_halted = bool(d.get("dd_halted"))
        self.axis_overrides = dict(d.get("axis_overrides") or {})
        for i in self.cfg.instruments:
            ov = self.axis_overrides.get(i.secid)
            if ov:
                if "don_enter" in ov:
                    i.don_enter = int(ov["don_enter"])
                if "don_exit" in ov:
                    i.don_exit = int(ov["don_exit"])
                if "atr_mult" in ov:
                    i.atr_mult = float(ov["atr_mult"])
                if "entry_notional_rub" in ov:
                    i.entry_notional_rub = float(ov["entry_notional_rub"])
        # миграция после фикса частичных баров 11.07: маркер, указывающий на НЕЗАКРЫТЫЙ
        # период (частичный бар успел обработаться), откатываем на 1мс — завершённая
        # версия бара переобработается, бэкфилл её не включит (bars ≤ last)
        now_ms = _now_ms_frame()
        for i in self.cfg.instruments:
            ts = self._last_bar_ts.get(i.secid)
            if ts and not bar_is_closed(ts, i.interval_min, now_ms):
                self._last_bar_ts[i.secid] = ts - 1
        self._saved_bar_ts = dict(self._last_bar_ts)   # база сравнения для персиста по тикам
        # восстановление открытых позиций — отложенно (движку нужен pv, ISS может лежать)
        self._pending_positions = dict(d.get("positions") or {})
        self._try_restore_positions()
        return True
