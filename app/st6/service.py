"""St6Session — сервисный слой фандинг-арбитража: реестр пар, дневной тик, исполнение.

Дневная гранулярность: решение принимается ОДИН раз на новый торговый день (когда в ISS
history появился вчерашний/сегодняшний SWAPRATE). Исполнение: paper (по settle) или
tbank_sandbox через St5PairExecutor (ноги разных лотов уже поддержаны этапом 2 st5;
«pref» = квартальник, «ord» = вечный: long_spread ≡ buy quart + sell perp — наш вход).
Состояние переживает рестарт (session_state_6.json).
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from .config import St6Config
from .data import days_to, near_quarterly, perp_history, point_value, quart_settle
from .engine import DaySnap, St6Engine, St6Position

# реестр пар: pid -> (perp_secid, quart_asset, label, perp_lots, quart_lots в юните).
# Лоты юнита выравнивают ₽-нотионалы ног: IMOEXF пункт 10₽ (нотионал ~23к) vs MX пункт 1₽
# (~233к) → 10:1; SBERF/GAZPF пункт 100₽ — нотионал равен квартальнику → 1:1.
ST6_PAIRS: dict[str, tuple] = {
    "imoexf": ("IMOEXF", "MIX", "Индекс ММВБ (вечный)", 10, 1),
    "sberf": ("SBERF", "SBRF", "Сбербанк (вечный)", 1, 1),
    "gazpf": ("GAZPF", "GAZR", "Газпром (вечный)", 1, 1),
}

EVENTS_LEN = 40


class St6Session:
    def __init__(self):
        self.cfg = St6Config()
        self.engines: dict[str, St6Engine] = {}
        self.trades: list[dict] = []
        self.missed: list[dict] = []              # журнал упущенных входов (аналитика)
        self.events: list[dict] = []
        self.enabled_pairs = {pid: True for pid in ST6_PAIRS}
        self.last_day: dict[str, str] = {}       # pid -> последний обработанный день ISS
        self.capital_rub: float = 0.0            # капитал sandbox-счёта (обновляется тиком)
        # якорь сверки «журнал vs счёт» (как st5): gap<0 = скрытые издержки/нет фандинга
        self.exec_anchor: dict | None = None
        self.edge_view: dict[str, dict] = {}      # pid -> снимок сигнала для UI
        self.state = {"live": False, "live_intent": False}
        self._session_file = Path(__file__).resolve().parent.parent.parent / "session_state_6.json"
        self._pv_cache: dict[str, float] = {}
        self._task = None

    # ---------- инициализация движков (ленивая: pv из ISS) ----------
    def _pv(self, secid: str) -> float:
        if secid not in self._pv_cache:
            self._pv_cache[secid] = point_value(secid)
        return self._pv_cache[secid]

    def _engine(self, pid: str) -> St6Engine:
        if pid not in self.engines:
            perp, qasset, _label, pl, ql = ST6_PAIRS[pid]
            # pv квартальника — по ближней серии (у всех серий актива он одинаков)
            qsec, _exp = near_quarterly(qasset)
            self.engines[pid] = St6Engine(pid, self.cfg.strategy,
                                          pv_perp=self._pv(perp), pv_quart=self._pv(qsec),
                                          perp_lots=pl, quart_lots=ql)
        return self.engines[pid]

    def log_event(self, kind: str, message: str) -> None:
        self.events.append({"ts": int(time.time() * 1000), "kind": kind, "message": message})
        if len(self.events) > EVENTS_LEN:
            del self.events[0]

    def log_missed(self, pid: str, day: str, fired: str, reason: str) -> None:
        """Журнал упущенных входов: сигнал был (fired), входа нет (reason). Один на пару/день."""
        if any(m["pair"] == pid and m["date"] == day for m in self.missed):
            return
        self.missed.append({"ts": int(time.time() * 1000), "date": day, "pair": pid,
                            "fired": fired, "reason": reason})
        if len(self.missed) > 100:
            del self.missed[0]

    # ---------- sandbox-исполнение (реальные ордера в песочнице T-Bank) ----------
    def _make_executor(self, perp_secid: str, quart_secid: str):
        """St5PairExecutor для пары st6: «ord» = вечный, «pref» = квартальник →
        наш вход (шорт перп + лонг кварт) = long_spread (buy pref + sell ord).
        None → paper-режим (исполнение по settle, без брокера)."""
        if self.cfg.mode != "tbank_sandbox" or not self.cfg.account_id:
            return None
        from ..st5.executor import St5PairExecutor
        return St5PairExecutor(self.cfg.account_id, perp_secid, quart_secid,
                               real=False, audit_cb=lambda a: self.log_event(
                                   "order", f"{a.get('op')} {a.get('direction')} "
                                            f"{a.get('lots')}лот {str(a.get('uid'))[:8]} "
                                            f"→ {a.get('status')}"))

    def _exec_enter(self, pid: str, eng: St6Engine, snap: DaySnap, units: int) -> bool:
        """Вход в брокере (sandbox). True — исполнено (или paper). False — отказ, входа нет."""
        perp = ST6_PAIRS[pid][0]
        ex = self._make_executor(perp, snap.quart_secid)
        if ex is None:
            return True
        try:
            ex.open_pair(True, units * eng.unit_perp, units * eng.unit_quart,
                         snap.perp_settle, snap.quart_settle)
            return True
        except Exception as e:  # noqa: BLE001
            self.log_event("warn", f"{pid}: вход в песочнице не удался: {e}")
            return False

    def _exec_exit(self, pid: str, eng: St6Engine, snap: DaySnap) -> bool:
        perp = ST6_PAIRS[pid][0]
        p = eng.position
        ex = self._make_executor(perp, p.quart_secid)
        if ex is None:
            return True
        try:
            ex.close_pair(True, p.perp_lots, p.quart_lots, snap.perp_settle, snap.quart_settle)
            return True
        except Exception as e:  # noqa: BLE001
            self.log_event("warn", f"{pid}: выход в песочнице не удался: {e}")
            return False

    def _exec_roll(self, pid: str, eng: St6Engine, snap: DaySnap,
                   old_settle: float) -> bool:
        """Ролл квартальной ноги: продать старую серию, купить новую (перп не трогаем)."""
        p = eng.position
        ex_old = self._make_executor(ST6_PAIRS[pid][0], p.quart_secid)
        if ex_old is None:
            return True
        ex_new = self._make_executor(ST6_PAIRS[pid][0], snap.quart_secid)
        try:
            uid_old = ex_old._uids()[1]
            ex_old._post(uid_old, p.quart_lots, "SELL", "roll", old_settle)
            uid_new = ex_new._uids()[1]
            ex_new._post(uid_new, p.quart_lots, "BUY", "roll", snap.quart_settle)
            return True
        except Exception as e:  # noqa: BLE001
            self.log_event("warn", f"{pid}: ролл в песочнице не удался: {e}")
            return False

    _go_cache: dict = {}

    def _unit_go(self, pid: str, perp_secid: str, quart_secid: str) -> float:
        """ГО одного юнита пары (ISS INITIALMARGIN обеих ног, без хедж-скидки биржи)."""
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

    def funding_history(self, days: int = 30) -> dict:
        """История аннуализированного фандинга по парам (для графика вкладки)."""
        out = {}
        for pid, (perp, _qa, label, _pl, _ql) in ST6_PAIRS.items():
            try:
                hist = perp_history(perp, days=days)
                out[pid] = {"label": label,
                            "points": [{"date": h["date"],
                                        "fund_ann_pp": round(h["swaprate"] / h["settle"] * 365 * 100, 2)}
                                       for h in hist if h["settle"] > 0]}
            except Exception as e:  # noqa: BLE001
                out[pid] = {"label": label, "error": str(e)[:120]}
        return out

    # ---------- дневной тик пары ----------
    def tick_pair(self, pid: str) -> dict:
        """Обработать пару: собрать снимок дня из ISS, шагнуть движок, исполнить действие.
        Возвращает снимок сигнала (для UI/edge-монитора). Идемпотентен внутри дня."""
        perp, qasset, label, _pl, _ql = ST6_PAIRS[pid]
        eng = self._engine(pid)
        st = self.cfg.strategy
        hist = perp_history(perp, days=st.fund_trail_days + 3)
        if not hist:
            return {"pair": pid, "error": "нет истории перпа"}
        day = hist[-1]["date"]
        # ближняя серия с учётом порога ролла: за roll_days_before до эксп. отдаст следующую
        qsec, qexp = near_quarterly(qasset, min_days_to_expiry=st.roll_days_before)
        qs, _qc = quart_settle(qsec)
        d2e = max(days_to(qexp), 1)
        perp_s = hist[-1]["settle"]
        trail = [h["swaprate"] for h in hist[-st.fund_trail_days:]]
        fund_ann = sum(trail) / len(trail) / perp_s * 365 * 100
        # базис к нотионал-паритету: цены ног в разных пунктах → сравниваем ₽-нотионалы юнита
        perp_notional = perp_s * eng.pv_perp * eng.unit_perp
        quart_notional = qs * eng.pv_quart * eng.unit_quart
        basis_ann = (quart_notional / perp_notional - 1) * 365 / d2e * 100
        snap = DaySnap(date=day, perp_settle=perp_s, swaprate=hist[-1]["swaprate"],
                       fund_trail_ann_pp=fund_ann, quart_secid=qsec, quart_settle=qs,
                       basis_ann_pp=basis_ann)
        view = {"pair": pid, "label": label, "date": day, "perp": perp, "quart": qsec,
                "fund_ann_pp": round(fund_ann, 1), "basis_ann_pp": round(basis_ann, 1),
                "edge_pp": round(fund_ann - basis_ann, 1), "d2e": d2e,
                "unit_go_rub": round(self._unit_go(pid, perp, qsec)),
                "swap_today_rub": round(hist[-1]["swaprate"] * eng.pv_perp * eng.unit_perp, 1),
                "in_position": eng.position is not None}
        self.edge_view[pid] = view
        if self.last_day.get(pid) == day:
            return view                            # день уже обработан — только обновили вид
        action = eng.daily_step(snap)
        self.last_day[pid] = day
        fired = (f"edge {view['edge_pp']:+.1f}пп > порог {st.edge_enter_pp} "
                 f"(фандинг {view['fund_ann_pp']:+.1f}% − базис {view['basis_ann_pp']:+.1f}%)")
        if action == "trap":
            self.log_missed(pid, day, fired,
                            f"дивидендная ловушка: |базис|={abs(view['basis_ann_pp']):.1f}пп "
                            f"> {st.basis_sane_pp} (дисконт не конвергирует в прибыль)")
            action = "none"
        # trading_enabled гейтит ТОЛЬКО вход (как в st4/st5): выходы/роллы работают всегда
        if action == "enter" and not (self.cfg.trading_enabled and self.enabled_pairs.get(pid, True)):
            self.log_missed(pid, day, fired,
                            "торговля выключена" if not self.cfg.trading_enabled else "пара выключена")
            action = "none"
        if action == "enter":
            if not self._exec_enter(pid, eng, snap, st.units):
                self.log_missed(pid, day, fired, "брокер: вход не исполнен (см. события)")
            else:
                fee = eng.pair_fee(eng.unit_perp * st.units, eng.unit_quart * st.units)
                eng.confirm_enter(snap, perp_fill=perp_s, quart_fill=qs, fee_rub=fee)
                eng.position.perp_secid = perp
                self.log_event("position", f"{pid}: ВХОД carry edge={view['edge_pp']}пп "
                                           f"(шорт {eng.position.perp_lots} {perp} + "
                                           f"лонг {eng.position.quart_lots} {qsec})")
        elif action == "exit":
            p = eng.position
            if self._exec_exit(pid, eng, snap):
                fee = eng.pair_fee(p.perp_lots, p.quart_lots)
                tr = eng.confirm_exit(snap, perp_fill=perp_s, quart_fill=qs, fee_rub=fee)
                self.trades.append(asdict(tr))
                self.log_event("exit", f"{pid}: ВЫХОД edge={view['edge_pp']}пп net {tr.net_pnl_rub:+.0f}₽ "
                                       f"(ноги {tr.legs_pnl_rub:+.0f} + фандинг {tr.funding_rub:+.0f} "
                                       f"− комиссии {tr.fees_rub:.0f})")
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
        for pid in ST6_PAIRS:
            if not self.enabled_pairs.get(pid, True):
                continue
            try:
                out.append(self.tick_pair(pid))
            except Exception as e:  # noqa: BLE001  одна пара не должна ронять остальные
                out.append({"pair": pid, "error": str(e)[:200]})
                self.log_event("warn", f"{pid}: тик не удался: {e}")
        self._refresh_capital()
        return out

    def _refresh_capital(self) -> None:
        """Капитал sandbox-счёта + якорь сверки. КРИТИЧНАЯ проверка st6: если песочница
        НЕ начисляет фандинг вечных, gap уедет в минус ровно на модельный фандинг."""
        if self.cfg.mode != "tbank_sandbox" or not self.cfg.account_id:
            return
        try:
            from ..st4 import tbank_sandbox as sb
            pf = sb.portfolio(self.cfg.account_id)
            total = sb._q_to_float(pf.get("totalAmountPortfolio"))
        except Exception:  # noqa: BLE001
            return
        if total and total > 0:
            self.capital_rub = float(total)
            if (self.exec_anchor is None
                    or self.exec_anchor.get("account_id") != self.cfg.account_id):
                net = sum(t.get("net_pnl_rub", 0) for t in self.trades)
                self.exec_anchor = {"account_id": self.cfg.account_id,
                                    "capital": float(total), "net": net}
                self.save_session()

    def _execution_gap(self) -> float | None:
        """Δфакт счёта − Δмодели (журнал + unrealized с фандингом) от якоря. None — нет данных."""
        a = self.exec_anchor
        if a is None or self.cfg.mode != "tbank_sandbox" or not self.capital_rub:
            return None
        if a.get("account_id") != self.cfg.account_id:
            return None
        net = sum(t.get("net_pnl_rub", 0) for t in self.trades)
        unreal = sum(e.unrealized_rub() for e in self.engines.values())
        return round((self.capital_rub - a.get("capital", 0.0))
                     - ((net + unreal) - a.get("net", 0.0)))

    # ---------- live-цикл ----------
    async def run_live(self) -> None:
        import asyncio
        self.log_event("info", f"ST6 live запущен ({len(ST6_PAIRS)} пар, режим {self.cfg.mode})")
        while self.state["live"]:
            try:
                self.tick_all()
            except Exception as e:  # noqa: BLE001
                self.log_event("warn", f"ST6 live ошибка: {e}")
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

    # ---------- снимок для UI ----------
    def snapshot(self) -> dict:
        positions = []
        for pid, eng in self.engines.items():
            p = eng.position
            if p is not None:
                positions.append({"pair": pid, "label": ST6_PAIRS[pid][2],
                                  "perp_lots": p.perp_lots, "quart_lots": p.quart_lots,
                                  "quart_secid": p.quart_secid, "entry_date": p.entry_date,
                                  "entry_edge_pp": p.entry_edge_pp,
                                  "funding_rub": round(p.funding_rub),
                                  "rolled": p.rolled,
                                  "unrealized_rub": round(eng.unrealized_rub())})
        net = sum(t.get("net_pnl_rub", 0) for t in self.trades)
        return {"live": self.state["live"], "mode": self.cfg.mode,
                "capital_rub": round(self.capital_rub) or None,
                "execution_gap_rub": self._execution_gap(),
                "account_id": self.cfg.account_id or None,
                "trading_enabled": self.cfg.trading_enabled,
                "enabled_pairs": self.enabled_pairs,
                "edge": list(self.edge_view.values()),
                "positions": positions, "trades": self.trades[-50:],
                "missed": self.missed[-50:],
                "net_pnl_rub": round(net), "events": self.events[-EVENTS_LEN:],
                "strategy": self.cfg.strategy.model_dump()}

    # ---------- персистентность ----------
    def save_session(self) -> None:
        try:
            data = {"config": self.cfg.model_dump(), "trades": self.trades,
                    "missed": self.missed[-100:], "exec_anchor": self.exec_anchor,
                    "last_day": self.last_day, "enabled_pairs": self.enabled_pairs,
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
        self.exec_anchor = data.get("exec_anchor") or None
        self.last_day = data.get("last_day", {})
        en = data.get("enabled_pairs") or {}
        self.enabled_pairs = {pid: bool(en.get(pid, True)) for pid in ST6_PAIRS}
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
            if pdict and pid in ST6_PAIRS:
                try:
                    self._engine(pid).position = St6Position(**pdict)
                except Exception:  # noqa: BLE001
                    pass
        self.state["live_intent"] = bool(data.get("live_intent", False))
        return True
