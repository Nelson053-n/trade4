"""Сервисный слой ST5 — ПОРТФЕЛЬНАЯ сессия (до 3 позиций на разные пары).

Отличие от st4 (1 сессия = 1 пара): St5Session держит ПОРТФЕЛЬ — по движку ST5Engine на
каждую пару-кандидата, общий портфельный риск (лимиты 0.5%/1.5%/5%, ≤3 позиций, ≤1 на
эмитента), общий live-цикл. Персистентность — session_state_5.json.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from ..st4 import data_feed as feed
from ..st4.models import Role
from .config import St5Config
from .engine import ST5Engine
from .models import St5State

_BASE = Path(__file__).resolve().parent.parent.parent
HISTORY_LEN = 300
EVENTS_LEN = 40

# пары-кандидаты ST5: (ord, pref, эмитент-ключ, ярлык). Один эмитент → ≤1 позиция (max_per_issuer).
ST5_PAIRS: dict[str, tuple] = {
    "sber": ("SBRF", "SBPR", "SBER", "Сбербанк"),
    "sngr": ("SNGR", "SNGP", "SNGR", "Сургутнефтегаз"),
    "tatn": ("TATN", "TATP", "TATN", "Татнефть"),
}


class St5Portfolio:
    """Портфельный менеджер: гейтит входы по лимитам и числу позиций."""

    def __init__(self, cfg: St5Config):
        self.cfg = cfg
        self.capital_rub: float = cfg.paper.start_balance_rub   # обновляется реальным портфелем в live
        self.day_pnl_rub: float = 0.0
        self.consecutive_errors: int = 0
        self.halted: bool = False
        self.halt_reason: str = ""
        self.pair_halted: dict[str, str] = {}    # per-pair HALT (изоляция отказа одной пары)

    def open_count(self, engines: dict[str, ST5Engine]) -> int:
        return sum(1 for e in engines.values() if e.position is not None)

    def open_issuers(self, engines: dict[str, ST5Engine], pairs: dict) -> set[str]:
        out = set()
        for pid, e in engines.items():
            if e.position is not None:
                out.add(pairs[pid][2])
        return out

    def can_open(self, pair: str, issuer: str, notional_rub: float,
                 engines: dict[str, ST5Engine], pairs: dict) -> tuple[bool, str]:
        """Разрешён ли вход в новую позицию по pair с заданным нотионалом. (ok, причина-отказа)."""
        r = self.cfg.risk
        if self.halted:
            return False, f"портфель HALTED: {self.halt_reason}"
        if pair in self.pair_halted:
            return False, f"пара HALTED: {self.pair_halted[pair]}"
        if not r.trading_enabled:
            return False, "торговля выключена"
        if self.open_count(engines) >= r.max_open_positions:
            return False, f"лимит позиций ({r.max_open_positions})"
        if issuer in self.open_issuers(engines, pairs):
            return False, f"уже есть позиция по эмитенту {issuer}"
        # лимит на сделку
        if notional_rub > r.risk_per_trade_pct * self.capital_rub:
            return False, "превышен лимит на сделку (0.5%)"
        # лимит на портфель: сумма нотионалов открытых + новая
        cur = sum(self._pos_notional(e) for e in engines.values() if e.position is not None)
        if cur + notional_rub > r.risk_per_portfolio_pct * self.capital_rub:
            return False, "превышен портфельный лимит (5%)"
        # дневной лимит убытка
        if self.day_pnl_rub <= -r.max_daily_loss_rub:
            return False, "дневной лимит убытка"
        return True, ""

    @staticmethod
    def _pos_notional(eng: ST5Engine) -> float:
        p = eng.position
        if p is None:
            return 0.0
        # грубо: нотионал в рублях ≈ |pref|·lots (пункты ≈ рубли на контракт по STEPPRICE=1 у этих пар)
        return abs(p.pref_entry) * p.lots

    def on_trade(self, net_pnl_rub: float) -> None:
        self.day_pnl_rub += net_pnl_rub

    def on_error(self) -> None:
        self.consecutive_errors += 1
        if self.consecutive_errors >= self.cfg.risk.max_consecutive_errors:
            self.halt("серия ошибок исполнения")

    def on_success(self) -> None:
        self.consecutive_errors = 0

    def halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason

    def halt_pair(self, pair: str, reason: str) -> None:
        self.pair_halted[pair] = reason

    def resume(self) -> None:
        self.halted = False
        self.halt_reason = ""
        self.consecutive_errors = 0


class St5Session:
    """Портфельная сессия ST5. Один экземпляр на ВЕСЬ портфель (не на пару)."""

    def __init__(self) -> None:
        self.cfg = St5Config()
        self._session_file = _BASE / "session_state_5.json"
        self.portfolio = St5Portfolio(self.cfg)
        # движок на каждую пару-кандидата
        self.engines: dict[str, ST5Engine] = {}
        self.specs: dict[str, tuple] = {}        # pid -> (spec_ord, spec_pref)
        for pid, spec in ST5_PAIRS.items():
            self.engines[pid] = ST5Engine(pid, self.cfg, base_lots=self.cfg.execution.quantity_lots)
        self.trades: list[dict] = []             # общий журнал портфеля (json-записи)
        self.history: dict[str, list] = {pid: [] for pid in ST5_PAIRS}   # история спреда по парам
        self.events: list[dict] = []
        self.state = {"live": False, "session_started": None, "paused_by_user": False,
                      "data_source": "synthetic", "sandbox_active": False,
                      "real_trading_armed": False}
        self.last_live_ts: dict[str, int] = {pid: 0 for pid in ST5_PAIRS}
        self._lock = asyncio.Lock()
        self._live_task = None

    # ---------- журнал событий ----------
    def log_event(self, kind: str, message: str) -> None:
        self.events.append({"ts": int(time.time() * 1000), "kind": kind, "message": message})
        if len(self.events) > EVENTS_LEN:
            del self.events[0]
        self.state["last_event"] = message

    def push_history(self, pid: str, ts: int) -> None:
        eng = self.engines[pid]
        if eng.last_z is None:
            return
        self.history[pid].append({
            "ts": ts, "spread": round(eng.last_spread, 2), "z": round(eng.last_z, 2),
            "beta": round(eng.last_beta, 4), "adf_p": round(eng.filt.adf_p, 3),
            "hurst": round(eng.filt.hurst, 2)})
        if len(self.history[pid]) > HISTORY_LEN:
            del self.history[pid][0]

    # ---------- снимок для UI ----------
    def snapshot(self) -> dict:
        positions = []
        for pid, eng in self.engines.items():
            p = eng.position
            if p is not None:
                positions.append({
                    "pair": pid, "label": ST5_PAIRS[pid][3], "state": p.state.value,
                    "entry_z": round(p.entry_z, 2), "lots": p.lots, "entry_lots": p.entry_lots,
                    "bars_held": p.bars_held, "partial_done": p.partial_done,
                    "unrealized_rub": round(eng.unrealized_rub(), 0),
                    "cur_z": round(eng.last_z, 2) if eng.last_z is not None else None})
        pairs_info = []
        for pid, eng in self.engines.items():
            pairs_info.append({
                "pair": pid, "label": ST5_PAIRS[pid][3],
                "z": round(eng.last_z, 2) if eng.last_z is not None else None,
                "beta": round(eng.last_beta, 4),
                "adf_p": round(eng.filt.adf_p, 3), "hurst": round(eng.filt.hurst, 2),
                "rv_ratio": round(eng.filt.rv_ratio, 2),
                "cointegrated": eng.filt.cointegrated, "mean_reverting": eng.filt.mean_reverting,
                "calm": eng.filt.calm_regime, "entry_allowed": eng.filt.entry_allowed(),
                "has_position": eng.position is not None,
                "halted": pid in self.portfolio.pair_halted})
        net = sum(t.get("net_pnl_rub", 0) for t in self.trades)
        return {
            "strategy": "st5", "live": self.state["live"],
            "session_started": self.state["session_started"],
            "data_source": self.state["data_source"],
            "sandbox_active": self.state["sandbox_active"],
            "real_trading_armed": self.state["real_trading_armed"],
            "connector_mode": self.cfg.connector.mode,
            "capital_rub": round(self.portfolio.capital_rub, 0),
            "day_pnl_rub": round(self.portfolio.day_pnl_rub, 0),
            "net_pnl_rub": round(net, 0),
            "halted": self.portfolio.halted, "halt_reason": self.portfolio.halt_reason,
            "trading_enabled": self.cfg.risk.trading_enabled,
            "open_positions": len(positions),
            "max_open_positions": self.cfg.risk.max_open_positions,
            "positions": positions, "pairs": pairs_info,
            "trades": self.trades[-100:], "events": self.events[-EVENTS_LEN:],
            "history": self.history,
            "limits": {"per_trade_pct": self.cfg.risk.risk_per_trade_pct,
                       "per_pair_pct": self.cfg.risk.risk_per_pair_pct,
                       "per_portfolio_pct": self.cfg.risk.risk_per_portfolio_pct},
        }

    # ---------- персистентность ----------
    def save_session(self) -> None:
        try:
            data = {
                "session_started": self.state["session_started"],
                "config": self.cfg.model_dump(),
                "trades": self.trades,
                "history": self.history,
                "day_pnl_rub": self.portfolio.day_pnl_rub,
                "capital_rub": self.portfolio.capital_rub,
                "live": self.state["live"],
                "paused_by_user": self.state["paused_by_user"],
                "data_source": self.state["data_source"],
                "last_live_ts": self.last_live_ts,
            }
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
        self.history = data.get("history", {pid: [] for pid in ST5_PAIRS})
        self.portfolio.day_pnl_rub = data.get("day_pnl_rub", 0.0)
        self.portfolio.capital_rub = data.get("capital_rub", self.cfg.paper.start_balance_rub)
        self.state["data_source"] = data.get("data_source", "synthetic")
        self.last_live_ts = data.get("last_live_ts", {pid: 0 for pid in ST5_PAIRS})
        # БЕЗОПАСНОСТЬ: рестарт ВСЕГДА снимает взвод реальной торговли (safe-by-default)
        self.state["real_trading_armed"] = False
        return True
