"""St7Session — сервисный слой «фандинг-давления». Переиспользует данные st6
(perp_history/near_quarterly/point_value) и исполнитель st5 (ноги разных лотов).
Дневной тик идемпотентен; журнал сделок и упущенных входов; персист session_state_7.json.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from ..st6.data import days_to, near_quarterly, perp_history, point_value, quart_settle
from ..st6.engine import DaySnap
from .config import St7Config
from .engine import St7Engine, St7Position

# реестр пар: pid -> (perp_secid, quart_asset, label, perp_lots, quart_lots в юните).
# Полухедж: перп 2×нотионал-паритет, квартальник 1× (половина направленного риска снята).
# IMOEXF пункт 10₽ (паритет 10:1 MX) → юнит 20:1; GAZPF пункт 100₽ (паритет 1:1 GZ) → 2:1.
# SBERF ИСКЛЮЧЁН: фандинг-сигнал на нём не работает (бэктест 444д ≈ 0).
# ВАЛЮТНЫЕ перпы (04.07, бэктест 365д): фандинг-машина — USDRUBF >35пп 245 дней/год
# (фандинг-компонент +48%/год нотионала на 2×шорт), EURRUBF 240 дней (+38%). Паритет:
# USDRUBF пункт 1000₽ (лот 1000$) ≈ Si-квартальник 1:1 → юнит 2:1; EURRUBF/Eu аналогично.
# GLDRUBF отложен: квартальник GOLD в $/oz — кросс-валютный хедж неточен.
ST7_PAIRS: dict[str, tuple] = {
    "imoexf": ("IMOEXF", "MIX", "Индекс ММВБ (давление)", 20, 1),
    "gazpf": ("GAZPF", "GAZR", "Газпром (давление)", 2, 1),
    "usdrubf": ("USDRUBF", "Si", "Доллар (давление)", 2, 1),
    "eurrubf": ("EURRUBF", "Eu", "Евро (давление)", 2, 1),
}

EVENTS_LEN = 40


class St7Session:
    def __init__(self):
        self.cfg = St7Config()
        self.engines: dict[str, St7Engine] = {}
        self.trades: list[dict] = []
        self.missed: list[dict] = []
        self.events: list[dict] = []
        self.enabled_pairs = {pid: True for pid in ST7_PAIRS}
        self.last_day: dict[str, str] = {}
        self.signal_view: dict[str, dict] = {}
        self.state = {"live": False, "live_intent": False}
        self._session_file = Path(__file__).resolve().parent.parent.parent / "session_state_7.json"
        self._pv_cache: dict[str, float] = {}
        self._go_cache: dict = {}
        self._task = None

    def _pv(self, secid: str) -> float:
        if secid not in self._pv_cache:
            self._pv_cache[secid] = point_value(secid)
        return self._pv_cache[secid]

    def _engine(self, pid: str) -> St7Engine:
        if pid not in self.engines:
            perp, qasset, _l, pl, ql = ST7_PAIRS[pid]
            qsec, _exp = near_quarterly(qasset)
            self.engines[pid] = St7Engine(pid, self.cfg.strategy,
                                          pv_perp=self._pv(perp), pv_quart=self._pv(qsec),
                                          perp_lots=pl, quart_lots=ql)
        return self.engines[pid]

    def log_event(self, kind: str, message: str) -> None:
        self.events.append({"ts": int(time.time() * 1000), "kind": kind, "message": message})
        if len(self.events) > EVENTS_LEN:
            del self.events[0]

    def log_missed(self, pid: str, day: str, fired: str, reason: str) -> None:
        if any(m["pair"] == pid and m["date"] == day for m in self.missed):
            return
        self.missed.append({"ts": int(time.time() * 1000), "date": day, "pair": pid,
                            "fired": fired, "reason": reason})
        if len(self.missed) > 100:
            del self.missed[0]

    def _unit_go(self, pid: str, perp_secid: str, quart_secid: str) -> float:
        key = (perp_secid, quart_secid)
        if key not in self._go_cache:
            try:
                from ..st4 import data_feed as feed
                eng = self._engine(pid)
                self._go_cache[key] = (feed.leg_margin(perp_secid) * eng.unit_perp
                                       + feed.leg_margin(quart_secid) * eng.unit_quart)
            except Exception:  # noqa: BLE001
                return 0.0
        return self._go_cache[key]

    # ---------- sandbox-исполнение (те же паттерны, что st6) ----------
    def _make_executor(self, perp_secid: str, quart_secid: str):
        if self.cfg.mode != "tbank_sandbox" or not self.cfg.account_id:
            return None
        from ..st5.executor import St5PairExecutor
        return St5PairExecutor(self.cfg.account_id, perp_secid, quart_secid,
                               real=False, audit_cb=lambda a: self.log_event(
                                   "order", f"{a.get('op')} {a.get('direction')} "
                                            f"{a.get('lots')}лот {str(a.get('uid'))[:8]} "
                                            f"→ {a.get('status')}"))

    def _exec_enter(self, pid: str, eng: St7Engine, snap: DaySnap, units: int) -> bool:
        ex = self._make_executor(ST7_PAIRS[pid][0], snap.quart_secid)
        if ex is None:
            return True
        try:
            ex.open_pair(True, units * eng.unit_perp, units * eng.unit_quart,
                         snap.perp_settle, snap.quart_settle)
            return True
        except Exception as e:  # noqa: BLE001
            self.log_event("warn", f"{pid}: вход в песочнице не удался: {e}")
            return False

    def _exec_exit(self, pid: str, eng: St7Engine, snap: DaySnap) -> bool:
        p = eng.position
        ex = self._make_executor(ST7_PAIRS[pid][0], p.quart_secid)
        if ex is None:
            return True
        try:
            ex.close_pair(True, p.perp_lots, p.quart_lots, snap.perp_settle, snap.quart_settle)
            return True
        except Exception as e:  # noqa: BLE001
            self.log_event("warn", f"{pid}: выход в песочнице не удался: {e}")
            return False

    def _exec_roll(self, pid: str, eng: St7Engine, snap: DaySnap, old_settle: float) -> bool:
        p = eng.position
        ex_old = self._make_executor(ST7_PAIRS[pid][0], p.quart_secid)
        if ex_old is None:
            return True
        ex_new = self._make_executor(ST7_PAIRS[pid][0], snap.quart_secid)
        try:
            ex_old._post(ex_old._uids()[1], p.quart_lots, "SELL", "roll", old_settle)
            ex_new._post(ex_new._uids()[1], p.quart_lots, "BUY", "roll", snap.quart_settle)
            return True
        except Exception as e:  # noqa: BLE001
            self.log_event("warn", f"{pid}: ролл в песочнице не удался: {e}")
            return False

    # ---------- дневной тик ----------
    def tick_pair(self, pid: str) -> dict:
        perp, qasset, label, _pl, _ql = ST7_PAIRS[pid]
        eng = self._engine(pid)
        st = self.cfg.strategy
        hist = perp_history(perp, days=st.fund_trail_days + 3)
        if not hist:
            return {"pair": pid, "error": "нет истории перпа"}
        day = hist[-1]["date"]
        qsec, qexp = near_quarterly(qasset, min_days_to_expiry=st.roll_days_before)
        qs, _qc = quart_settle(qsec)
        d2e = max(days_to(qexp), 1)
        perp_s = hist[-1]["settle"]
        trail = [h["swaprate"] for h in hist[-st.fund_trail_days:]]
        fund_ann = sum(trail) / len(trail) / perp_s * 365 * 100
        perp_notional = perp_s * eng.pv_perp * eng.unit_perp
        quart_notional = qs * eng.pv_quart * eng.unit_quart
        # базис к ПАРИТЕТНОЙ доле хеджа (кварт 1× против перпа 2×: сравниваем с половиной)
        basis_ann = (quart_notional / (perp_notional / 2) - 1) * 365 / d2e * 100
        snap = DaySnap(date=day, perp_settle=perp_s, swaprate=hist[-1]["swaprate"],
                       fund_trail_ann_pp=fund_ann, quart_secid=qsec, quart_settle=qs,
                       basis_ann_pp=basis_ann)
        view = {"pair": pid, "label": label, "date": day, "perp": perp, "quart": qsec,
                "fund_ann_pp": round(fund_ann, 1), "basis_ann_pp": round(basis_ann, 1),
                "enter_at_pp": st.fund_enter_pp, "exit_at_pp": st.fund_exit_pp, "d2e": d2e,
                "unit_go_rub": round(self._unit_go(pid, perp, qsec)),
                "swap_today_rub": round(hist[-1]["swaprate"] * eng.pv_perp * eng.unit_perp, 1),
                "in_position": eng.position is not None}
        self.signal_view[pid] = view
        if self.last_day.get(pid) == day:
            return view
        action = eng.daily_step(snap)
        self.last_day[pid] = day
        fired = (f"фандинг {view['fund_ann_pp']:+.1f}% > порог {st.fund_enter_pp} "
                 f"(давление толпы в лонгах)")
        if action == "trap":
            self.log_missed(pid, day, fired,
                            f"дивидендная аномалия базиса ({view['basis_ann_pp']:+.1f}пп)")
            action = "none"
        if action == "gap_block":
            self.log_missed(pid, day, fired,
                            f"🚨 широкий гэп перпа >{st.gap_block_pct}% против шорта — вход заблокирован")
            self.log_event("warn", f"{pid}: вход заблокирован защитой от гэпа")
            action = "none"
        # дневной лимит убытка (HALT новых входов при накопленном минусе за день)
        if action == "enter" and st.daily_loss_limit_rub > 0:
            import datetime as _dtm
            _msk = _dtm.timezone(_dtm.timedelta(hours=3))
            day_net = sum(t.get("net_pnl_rub", 0) for t in self.trades
                          if t.get("exit_date") == day)
            if day_net < -abs(st.daily_loss_limit_rub):
                self.log_missed(pid, day, fired,
                                f"🚨 дневной лимит убытка {st.daily_loss_limit_rub:.0f}₽ достигнут "
                                f"(день {day_net:+.0f}₽) — входы остановлены")
                action = "none"
        if action == "enter" and not (self.cfg.trading_enabled and self.enabled_pairs.get(pid, True)):
            self.log_missed(pid, day, fired,
                            "торговля выключена" if not self.cfg.trading_enabled else "пара выключена")
            action = "none"
        if action == "stop":
            # АВАРИЙНЫЙ ВЫХОД по стоп-лоссу (защита от девальвационного гэпа) — то же
            # исполнение, что exit, но причина 'stop' и 🚨 TG-нотификация
            p = eng.position
            if self._exec_exit(pid, eng, snap):
                fee = eng.pair_fee(p.perp_lots, p.quart_lots)
                tr = eng.confirm_exit(snap, perp_fill=perp_s, quart_fill=qs, fee_rub=fee, reason="stop")
                self.trades.append(asdict(tr))
                self.log_event("warn", f"🚨 {pid}: СТОП-ЛОСС net {tr.net_pnl_rub:+.0f}₽ "
                                       f"(убыток > {st.stop_loss_pct}% нотионала — защита от гэпа)")
            self.save_session()
            view["in_position"] = eng.position is not None
            return view
        if action == "enter":
            if not self._exec_enter(pid, eng, snap, st.units):
                self.log_missed(pid, day, fired, "брокер: вход не исполнен (см. события)")
            else:
                fee = eng.pair_fee(eng.unit_perp * st.units, eng.unit_quart * st.units)
                eng.confirm_enter(snap, perp_fill=perp_s, quart_fill=qs, fee_rub=fee)
                eng.position.perp_secid = perp
                self.log_event("position",
                               f"{pid}: ВХОД давление фандинг={view['fund_ann_pp']}% "
                               f"(шорт {eng.position.perp_lots} {perp} + "
                               f"полухедж {eng.position.quart_lots} {qsec})")
        elif action == "exit":
            p = eng.position
            if self._exec_exit(pid, eng, snap):
                fee = eng.pair_fee(p.perp_lots, p.quart_lots)
                tr = eng.confirm_exit(snap, perp_fill=perp_s, quart_fill=qs, fee_rub=fee)
                self.trades.append(asdict(tr))
                self.log_event("exit", f"{pid}: ВЫХОД фандинг={view['fund_ann_pp']}% "
                                       f"net {tr.net_pnl_rub:+.0f}₽ (ноги {tr.legs_pnl_rub:+.0f} "
                                       f"+ фандинг {tr.funding_rub:+.0f} − комиссии {tr.fees_rub:.0f})")
        elif action == "roll":
            p = eng.position
            old_settle, _ = quart_settle(p.quart_secid)
            if self._exec_roll(pid, eng, snap, old_settle):
                fee = eng.pair_fee(0, p.quart_lots) * 2
                eng.confirm_roll(snap, old_quart_fill=old_settle, new_quart_fill=qs, fee_rub=fee)
                self.log_event("info", f"{pid}: ролл хеджа → {qsec}")
        self.save_session()
        view["in_position"] = eng.position is not None
        return view

    def tick_all(self) -> list[dict]:
        out = []
        for pid in ST7_PAIRS:
            if not self.enabled_pairs.get(pid, True):
                continue
            try:
                out.append(self.tick_pair(pid))
            except Exception as e:  # noqa: BLE001
                out.append({"pair": pid, "error": str(e)[:200]})
                self.log_event("warn", f"{pid}: тик не удался: {e}")
        return out

    async def run_live(self) -> None:
        import asyncio
        self.log_event("info", f"ST7 live запущен ({len(ST7_PAIRS)} пар, режим {self.cfg.mode})")
        while self.state["live"]:
            try:
                self.tick_all()
            except Exception as e:  # noqa: BLE001
                self.log_event("warn", f"ST7 live ошибка: {e}")
            await asyncio.sleep(self.cfg.poll_seconds)

    def start_live(self) -> None:
        import asyncio
        self.state["live"] = True
        self.state["live_intent"] = True
        self.save_session()
        if self._task is not None and not self._task.done():
            return
        try:
            self._task = asyncio.create_task(self.run_live())
        except RuntimeError:
            self._task = None

    def stop_live(self) -> None:
        self.state["live"] = False
        self.state["live_intent"] = False
        self.save_session()

    def snapshot(self) -> dict:
        positions = []
        for pid, eng in self.engines.items():
            p = eng.position
            if p is not None:
                positions.append({"pair": pid, "label": ST7_PAIRS[pid][2],
                                  "perp_lots": p.perp_lots, "quart_lots": p.quart_lots,
                                  "quart_secid": p.quart_secid, "entry_date": p.entry_date,
                                  "entry_fund_pp": p.entry_fund_pp,
                                  "funding_rub": round(p.funding_rub), "rolled": p.rolled,
                                  "unrealized_rub": round(eng.unrealized_rub())})
        net = sum(t.get("net_pnl_rub", 0) for t in self.trades)
        return {"live": self.state["live"], "mode": self.cfg.mode,
                "account_id": self.cfg.account_id or None,
                "trading_enabled": self.cfg.trading_enabled,
                "enabled_pairs": self.enabled_pairs,
                "signals": list(self.signal_view.values()),
                "positions": positions, "trades": self.trades[-50:],
                "missed": self.missed[-50:],
                "net_pnl_rub": round(net), "events": self.events[-EVENTS_LEN:],
                "strategy": self.cfg.strategy.model_dump()}

    def save_session(self) -> None:
        try:
            data = {"config": self.cfg.model_dump(), "trades": self.trades,
                    "missed": self.missed[-100:], "last_day": self.last_day,
                    "enabled_pairs": self.enabled_pairs,
                    "live_intent": self.state.get("live_intent", False),
                    "positions": {pid: (asdict(e.position) if e.position else None)
                                  for pid, e in self.engines.items()}}
            self._session_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception:  # noqa: BLE001
            pass

    def load_session(self) -> bool:
        if not self._session_file.exists():
            return False
        try:
            data = json.loads(self._session_file.read_text())
        except Exception:  # noqa: BLE001
            return False
        self.trades = data.get("trades", [])
        self.missed = list(data.get("missed") or [])
        self.last_day = data.get("last_day", {})
        en = data.get("enabled_pairs") or {}
        self.enabled_pairs = {pid: bool(en.get(pid, True)) for pid in ST7_PAIRS}
        st = (data.get("config") or {}).get("strategy")
        if isinstance(st, dict):
            try:
                self.cfg.strategy = type(self.cfg.strategy)(**st)
            except Exception:  # noqa: BLE001
                pass
        for k in ("mode", "account_id", "trading_enabled"):
            v = (data.get("config") or {}).get(k)
            if v is not None:
                setattr(self.cfg, k, v)
        for pid, pdict in (data.get("positions") or {}).items():
            if pdict and pid in ST7_PAIRS:
                try:
                    self._engine(pid).position = St7Position(**pdict)
                except Exception:  # noqa: BLE001
                    pass
        self.state["live_intent"] = bool(data.get("live_intent", False))
        return True
