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
from .engine import St9Engine, Bar

ISS = "https://iss.moex.com/iss"
EVENTS_LEN = 60


def _iss(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "trade4-st9"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def iss_candles_60m(secid: str, frm: str) -> list[Bar]:
    """ЗАКРЫТЫЕ 60м свечи фьючерса с ISS (формирующийся бар отброшен)."""
    try:
        d = _iss(f"{ISS}/engines/futures/markets/forts/securities/{secid}/candles.json"
                 f"?iss.meta=off&interval=60&from={frm}")
        ci = {c: i for i, c in enumerate(d["candles"]["columns"])}
        # ISS отдаёт naive-даты в МСК → сравниваем с naive-МСК (aware vs naive = TypeError)
        now = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=3)
        out = []
        for r in d["candles"]["data"]:
            end = datetime.fromisoformat(r[ci["end"]])
            if end > now:            # бар ещё формируется
                continue
            ts = int(datetime.fromisoformat(r[ci["begin"]]).timestamp() * 1000)
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
        self.capital_rub: float = 0.0
        self.exec_anchor: dict | None = None
        self._task = None

    def log_event(self, kind: str, message: str) -> None:
        self.events.append({"ts": int(time.time() * 1000), "kind": kind, "message": message})
        if len(self.events) > EVENTS_LEN:
            del self.events[0]

    def _pv(self, secid: str) -> float:
        if secid not in self._pv_cache:
            try:
                from ..st6.data import point_value
                self._pv_cache[secid] = float(point_value(secid))
            except Exception:  # noqa: BLE001
                return 1.0
        return self._pv_cache[secid]

    def _engine(self, icfg) -> St9Engine:
        if icfg.secid not in self.engines:
            s = self.cfg.strategy
            self.engines[icfg.secid] = St9Engine(
                icfg.secid, icfg.don_enter, icfg.don_exit, icfg.atr_mult,
                s.atr_period, pv=self._pv(icfg.secid),
                fee_per_lot=s.fee_per_lot, allow_short=s.allow_short)
        return self.engines[icfg.secid]

    # ---------- исполнение (перп, один инструмент — атомарность не нужна) ----------
    def _order(self, secid: str, lots: int, direction: str) -> None:
        """Market-ордер в песочницу (или paper-ничего). Мелкими по 1 лоту (ёмкость)."""
        if self.cfg.mode != "tbank_sandbox" or not self.cfg.account_id:
            return
        import uuid as _uuid
        from ..st4 import tbank_sandbox as sb
        uid = sb.find_future(secid)["uid"]
        for _ in range(lots):
            sb.post_order(self.cfg.account_id, uid, 1,
                          f"ORDER_DIRECTION_{direction}", str(_uuid.uuid4()))

    def _apply_signal(self, eng: St9Engine, sig: dict, icfg) -> None:
        ts = int(time.time() * 1000)
        try:
            if sig["act"] in ("close", "reverse"):
                closing = eng.position
                self._order(eng.secid, closing.lots,
                            "SELL" if closing.side == "long" else "BUY")
                tr = eng.close(sig["px"], ts, sig["reason"])
                self.trades.append(tr.__dict__)
                self.log_event("exit", f"{eng.secid}: выход {tr.side} ({tr.reason}) "
                                       f"net {tr.net_pnl_rub:+.0f}₽")
            if sig["act"] in ("open", "reverse") and self.cfg.trading_enabled:
                side = sig["new_side"]
                lots = max(1, int(icfg.entry_notional_rub / (sig["px"] * eng.pv)))
                self._order(eng.secid, lots, "BUY" if side == "long" else "SELL")
                eng.open(side, sig["px"], lots, ts, sig["atr"])
                self.log_event("position", f"{eng.secid}: {side.upper()} {lots}лот @ {sig['px']}")
            self.save_session()
        except Exception as e:  # noqa: BLE001
            self.log_event("warn", f"{eng.secid}: исполнение не удалось: {str(e)[:80]}")

    # ---------- тик ----------
    def tick(self) -> dict:
        acted = {"signals": 0}
        for icfg in self.cfg.instruments:
            eng = self._engine(icfg)
            # качаем с последнего известного бара; полные 14 дней — на прогреве ИЛИ
            # когда движок пуст после рестарта (нужен бэкфилл индикаторов)
            last0 = self._last_bar_ts.get(icfg.secid, 0)
            need_backfill = last0 > 0 and not eng.bars
            frm = (datetime.fromtimestamp(last0 / 1000).strftime("%Y-%m-%d")
                   if last0 and not need_backfill
                   else (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d"))
            bars = iss_candles_60m(icfg.secid, frm)
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
            warmup = last == 0    # первый запуск: только прогрев индикаторов,
            for b in fresh:       # БЕЗ сделок (иначе журнал засоряют фиктивные входы истории)
                self._last_bar_ts[icfg.secid] = b.ts
                lots = max(1, int(icfg.entry_notional_rub / (b.c * eng.pv))) if b.c > 0 else 1
                sig = eng.step(b, lots_for_entry=lots)
                if sig and not warmup:
                    acted["signals"] += 1
                    self._apply_signal(eng, sig, icfg)
            if warmup and fresh:
                eng.position = None   # стартуем flat; вход по следующему реальному пробою
                self.log_event("info", f"{icfg.secid}: прогрет ({len(fresh)} баров), старт flat")
        self.refresh_capital()
        return acted

    def refresh_capital(self) -> None:
        if self.cfg.mode != "tbank_sandbox" or not self.cfg.account_id:
            return
        try:
            from ..st4 import tbank_sandbox as sb
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
                await asyncio.to_thread(self.tick)
            except Exception as e:  # noqa: BLE001
                self.log_event("warn", f"тик не удался: {str(e)[:100]}")
            await asyncio.sleep(self.cfg.poll_seconds)

    def start_live(self) -> None:
        import asyncio
        if self.state["live"]:
            return
        self.state["live"] = True
        self.state["live_intent"] = True
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
            "capital_rub": round(self.capital_rub) or None,
            "events": self.events[-20:],
        }

    def save_session(self) -> None:
        try:
            data = {"config": self.cfg.model_dump(), "trades": self.trades,
                    "state": self.state, "last_bar_ts": self._last_bar_ts,
                    "exec_anchor": self.exec_anchor,
                    # позиции ПЕРСИСТЯТСЯ (грабли st5: рестарт терял открытые позиции)
                    "positions": {sec: e.position.__dict__
                                  for sec, e in self.engines.items() if e.position}}
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
        self._last_bar_ts = {k: int(v) for k, v in (d.get("last_bar_ts") or {}).items()}
        self.exec_anchor = d.get("exec_anchor") or None
        cfg = d.get("config")
        if cfg:
            try:
                self.cfg = St9Config(**cfg)
            except Exception:  # noqa: BLE001
                pass
        # восстановление открытых позиций в движки (сами движки создаются лениво)
        from .engine import St9Position
        for sec, pd in (d.get("positions") or {}).items():
            icfg = next((i for i in self.cfg.instruments if i.secid == sec), None)
            if icfg is None:
                continue
            try:
                self._engine(icfg).position = St9Position(**pd)
            except Exception:  # noqa: BLE001
                self.log_event("warn", f"{sec}: позиция из session не восстановлена")
        return True
