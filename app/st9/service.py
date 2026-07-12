"""St9Session — сервис «трендовой корзины»: 60м бары перпов с ISS, Donchian+ATR движки,
исполнение paper/sandbox (переиспользует tbank_sandbox), учёт по кэшу счёта.
"""
from __future__ import annotations

import json
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
    def __init__(self):
        self.cfg = St9Config()
        self.engines: dict[str, St9Engine] = {}
        self.trades: list[dict] = []
        self.events: list[dict] = []
        self.state = {"live": False, "live_intent": False}
        self._session_file = Path(__file__).resolve().parent.parent.parent / "session_state_9.json"
        self._last_bar_ts: dict[str, int] = {}
        self._pv_cache: dict[str, float] = {}
        self._pv_warned: set[str] = set()             # анти-спам warn'ов «pv недоступен»
        self._contract_cache: dict[str, tuple] = {}   # asset -> (secid, дата резолва)
        self._bars_contract: dict[str, str] = {}      # asset -> контракт, чьи бары в движке
        self._pending_positions: dict[str, dict] = {}  # позиции session, ждущие движка (pv)
        self.contracts: dict[str, str] = {}           # asset -> контракт ОТКРЫТОЙ позиции (персист)
        self.capital_rub: float = 0.0
        self.exec_anchor: dict | None = None
        self.last_tick_ts: int = 0                    # мс; наблюдаемость живости цикла
        self._hb_ts: float = 0.0                      # heartbeat-событие раз в час
        self._task = None

    def log_event(self, kind: str, message: str) -> None:
        self.events.append({"ts": int(time.time() * 1000), "kind": kind, "message": message})
        if len(self.events) > EVENTS_LEN:
            del self.events[0]

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
                fee_per_lot=s.fee_per_lot, allow_short=s.allow_short)
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

    # ---------- исполнение (перп, один инструмент — атомарность не нужна) ----------
    def _order(self, secid: str, lots: int, direction: str, ref_px: float = 0.0) -> int:
        """Market-ордера по 1 лоту (ёмкость). Возвращает ФАКТИЧЕСКИ исполненные лоты:
        отказ в середине серии не должен оставлять слепые лоты (движок ≠ счёт).

        ⚠️ tbank_real: гейт на КАЖДЫЙ ордер (вход/выход/ролл) — armed+cooldown; идемпотентный
        orderId (ретрай не задвоит); sanity цены против ref_px (сигнальный бар)."""
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
        for i in range(lots):
            try:
                if real:
                    raw = f"{self.cfg.account_id}|{uid}|1|{direction}|{i}|{int(time.time())}"
                    oid = _hl.sha256(raw.encode()).hexdigest()[:32]   # идемпотентный
                    resp = live.post_order(self.cfg.account_id, uid, 1,
                                           f"ORDER_DIRECTION_{direction}", oid)
                else:
                    resp = sb.post_order(self.cfg.account_id, uid, 1,
                                         f"ORDER_DIRECTION_{direction}", str(_uuid.uuid4()))
            except Exception as e:  # noqa: BLE001
                self.log_event("warn", f"{secid}: ордер {direction} прерван "
                                       f"на {filled}/{lots}: {str(e)[:60]}")
                break
            v = resp.get("lotsExecuted")
            if v is None:
                v = resp.get("executedLots")
            try:
                filled += int(float(v)) if v is not None else 1
            except (TypeError, ValueError):
                filled += 1
        return filled

    def _trade_secid(self, icfg) -> str:
        """Что реально торгуем: перп = secid; квартальник = текущий контракт."""
        return (self._resolve_contract(icfg) or icfg.secid) if icfg.quarterly else icfg.secid

    def _entry_lots(self, icfg, px: float, pv: float) -> int:
        """Лоты входа из нотионала оси; в tbank_real нотионал режется потолком
        real_max_notional_rub (боевой лимит объёма — пилот на малом размере)."""
        if px <= 0 or pv <= 0:
            return 1
        target = icfg.entry_notional_rub
        cap = getattr(self.cfg.strategy, "real_max_notional_rub", 0.0)
        if self.cfg.mode == "tbank_real" and cap > 0:
            target = min(target, cap)
        return max(1, int(target / (px * pv)))

    def _apply_signal(self, eng: St9Engine, sig: dict, icfg) -> None:
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
                    # частичное закрытие: движок ведёт ОСТАТОК (трейл продолжает защищать),
                    # сделка не фиксируется — P&L закрытой части покажет счёт (execution_gap)
                    closing.lots -= got
                    self.log_event("warn", f"🚨 {eng.secid}: закрыто {got} лотов, остаток "
                                           f"{closing.lots} — выход повторится следующим баром")
                    self.save_session()
                    return
                tr = eng.close(sig["px"], ts, sig["reason"])
                self.trades.append(tr.__dict__)
                self.contracts.pop(icfg.secid, None)
                self.log_event("exit", f"{eng.secid}: выход {tr.side} ({tr.reason}) "
                                       f"net {tr.net_pnl_rub:+.0f}₽")
            if sig["act"] in ("open", "reverse") and self.cfg.trading_enabled:
                side = sig["new_side"]
                if icfg.quarterly:
                    pv = self._pv(sec)       # pv контракта (может отличаться между сериями)
                    if pv is None:
                        self.log_event("warn", f"{eng.secid}: pv {sec} недоступен — вход пропущен")
                        self.save_session()
                        return
                    eng.pv = pv
                lots = self._entry_lots(icfg, sig["px"], eng.pv)
                got = self._order(sec, lots, "BUY" if side == "long" else "SELL",
                                  ref_px=sig["px"])
                if got <= 0:
                    self.log_event("warn", f"{eng.secid}: вход не исполнен (0 лотов налито)")
                else:
                    eng.open(side, sig["px"], got, ts, sig["atr"])
                    if icfg.quarterly:
                        self.contracts[icfg.secid] = sec
                    self.log_event("position", f"{eng.secid}: {side.upper()} {got}лот"
                                               f"{' '+sec if sec!=eng.secid else ''} @ {sig['px']}")
            self.save_session()
        except Exception as e:  # noqa: BLE001
            self.log_event("warn", f"{eng.secid}: исполнение не удалось: {str(e)[:80]}")

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
                p.lots -= got
                self.log_event("warn", f"🚨 {eng.secid}: ролл прерван — закрыто {got}, "
                                       f"остаток {p.lots} на {old_sec}, повтор следующим тиком")
                self.save_session()
                return
            tr = eng.close(old_px, ts, "roll")
            self.trades.append(tr.__dict__)
            eng.pv = new_pv
            lots = self._entry_lots(icfg, new_px, new_pv)
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
            self.log_event("warn", f"{eng.secid}: ролл не удался: {str(e)[:80]}")

    # ---------- тик ----------
    def tick(self) -> dict:
        acted = {"signals": 0}
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
                if eng.position is not None and held_c and held_c != fresh_c:
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
                self._last_bar_ts[icfg.secid] = b.ts
                lots = self._entry_lots(icfg, b.c, eng.pv)
                sig = eng.step(b, lots_for_entry=lots)
                if sig and not warmup:
                    acted["signals"] += 1
                    self._apply_signal(eng, sig, icfg)
            if warmup and fresh:
                # position и так None (warmup только во флэте) — стирать НЕЛЬЗЯ:
                # прежний безусловный сброс убивал позицию после ролла (ревизия 11.07)
                self.log_event("info", f"{icfg.secid}: прогрет ({len(fresh)} баров), старт flat")
        self.refresh_capital()
        self.last_tick_ts = int(time.time() * 1000)
        if time.time() - self._hb_ts > 3600:          # heartbeat: тики st9 тихие,
            self._hb_ts = time.time()                 # без него живость не видна
            npos = sum(1 for e in self.engines.values() if e.position)
            self.log_event("info", f"цикл жив: {len(self.cfg.instruments)} осей, позиций {npos}")
        return acted

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
                if self.exec_anchor is None:
                    net = sum(t.get("net_pnl_rub", 0) for t in self.trades)
                    self.exec_anchor = {"capital": float(total), "net": net,
                                        "account_id": self.cfg.account_id}
        except Exception:  # noqa: BLE001
            pass

    async def run_live(self) -> None:
        import asyncio
        self.state["live"] = True
        while self.state["live"]:
            try:
                # wait_for: зависший тик (DNS-фаза вне urllib-timeout) не убивает цикл
                await asyncio.wait_for(asyncio.to_thread(self.tick),
                                       timeout=max(120.0, self.cfg.poll_seconds * 0.9))
            except asyncio.TimeoutError:
                self.log_event("warn", "тик завис (таймаут) — пропущен, цикл жив")
            except Exception as e:  # noqa: BLE001
                self.log_event("warn", f"тик не удался: {str(e)[:100]}")
            await asyncio.sleep(self.cfg.poll_seconds)

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
            "events": self.events[-20:],
        }

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
                    "positions": pos}
            self._session_file.write_text(json.dumps(data, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass

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
        cfg = d.get("config")
        if cfg:
            try:
                self.cfg = St9Config(**cfg)
                # РЕЕСТР ИНСТРУМЕНТОВ — ИЗ КОДА, не из session (как ST4_PAIRS): иначе
                # добавленная в код ось затирается старым сохранённым списком
                # (ловушка 09.07: GAZR исчез после рестарта — файл был от v1 с 2 осями)
                self.cfg.instruments = St9Config().instruments
            except Exception:  # noqa: BLE001
                pass
        # миграция после фикса частичных баров 11.07: маркер, указывающий на НЕЗАКРЫТЫЙ
        # период (частичный бар успел обработаться), откатываем на 1мс — завершённая
        # версия бара переобработается, бэкфилл её не включит (bars ≤ last)
        now_ms = _now_ms_frame()
        for i in self.cfg.instruments:
            ts = self._last_bar_ts.get(i.secid)
            if ts and not bar_is_closed(ts, i.interval_min, now_ms):
                self._last_bar_ts[i.secid] = ts - 1
        # восстановление открытых позиций — отложенно (движку нужен pv, ISS может лежать)
        self._pending_positions = dict(d.get("positions") or {})
        self._try_restore_positions()
        return True
