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
    # (ord, pref, эмитент, ярлык, опц. per-pair оверрайды StrategyConfig).
    # Оверрайды откалиброваны sweep'ом на 365д (по Sharpe, ≥5 сделок). КАЖДАЯ пара статистически
    # разная — глобальные параметры ломают часть пар (напр. z_exit=0.1 хорош sber/tatn, но sngr
    # лучше с 0.35). Поэтому per-pair, как в st4.
    # z_entry=1.25 (аудит 2026-06-26: OOS-проверено, +50-60% net в ОБЕИХ половинах всех пар,
    # maxDD не вырос). Тиры сайзинга сдвинуты под z_entry (иначе вход разрешён, но size=None).
    "sber": ("SBRF", "SBPR", "SBER", "Сбербанк",
             {"z_entry": 1.25, "z_take_partial": 1.5, "z_exit_full": 0.1,
              "size_tiers": [(1.25, 1.75, 1.0), (1.75, 2.25, 1.5), (2.25, 4.0, 2.0)]}),
    "sngr": ("SNGR", "SNGP", "SNGR", "Сургутнефтегаз",
             {"z_entry": 1.25, "z_take_partial": 1.5, "z_exit_full": 0.35,
              "size_tiers": [(1.25, 1.75, 1.0), (1.75, 2.25, 1.5), (2.25, 4.0, 2.0)]}),
    "tatn": ("TATN", "TATP", "TATN", "Татнефть",
             {"z_entry": 1.25, "z_take_partial": 1.25, "z_exit_full": 0.1,
              "size_tiers": [(1.25, 1.75, 1.0), (1.75, 2.25, 1.5), (2.25, 4.0, 2.0)]}),
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

    def open_count(self, engines: dict[str, ST5Engine], exclude: str | None = None) -> int:
        return sum(1 for pid, e in engines.items()
                   if e.position is not None and pid != exclude)

    def open_issuers(self, engines: dict[str, ST5Engine], pairs: dict,
                     exclude: str | None = None) -> set[str]:
        out = set()
        for pid, e in engines.items():
            if e.position is not None and pid != exclude:
                out.add(pairs[pid][2])
        return out

    def can_open(self, pair: str, issuer: str, risk_rub: float,
                 engines: dict[str, ST5Engine], pairs: dict) -> tuple[bool, str]:
        """Разрешён ли вход в новую позицию по pair. (ok, причина). risk_rub = ГО позиции
        (заблокированное обеспечение), НЕ нотионал — корректная мера риска для фьючерсов.

        ВАЖНО: движок к этому моменту уже выставил eng.position кандидата → исключаем pair
        из подсчёта открытых (иначе сам себя блокирует как «уже есть позиция по эмитенту»)."""
        r = self.cfg.risk
        if self.halted:
            return False, f"портфель HALTED: {self.halt_reason}"
        if pair in self.pair_halted:
            return False, f"пара HALTED: {self.pair_halted[pair]}"
        if not r.trading_enabled:
            return False, "торговля выключена"
        if self.open_count(engines, exclude=pair) >= r.max_open_positions:
            return False, f"лимит позиций ({r.max_open_positions})"
        if issuer in self.open_issuers(engines, pairs, exclude=pair):
            return False, f"уже есть позиция по эмитенту {issuer}"
        # лимит ГО на сделку (% капитала)
        if risk_rub > r.risk_per_trade_pct * self.capital_rub:
            return False, f"ГО сделки {risk_rub:.0f}₽ > лимит {r.risk_per_trade_pct*100:.1f}% ({r.risk_per_trade_pct*self.capital_rub:.0f}₽)"
        # лимит ГО на портфель: сумма ГО УЖЕ открытых (кроме кандидата) + новая
        cur = sum(self._pos_risk(pid, e) for pid, e in engines.items()
                  if e.position is not None and pid != pair)
        if cur + risk_rub > r.risk_per_portfolio_pct * self.capital_rub:
            return False, "превышен портфельный лимит ГО"
        # дневной лимит убытка
        if self.day_pnl_rub <= -r.max_daily_loss_rub:
            return False, "дневной лимит убытка"
        return True, ""

    # кэш ГО пары (leg_margin обеих ног, ₽ за 1 лот) — меняется редко, биржа раз в день
    _go_cache: dict = {}

    @classmethod
    def pair_go_per_lot(cls, pid: str) -> float:
        """ГО пары на 1 лот (обе ноги) из ISS leg_margin. Это РИСК на сделку для фьючерсов
        (заблокированное обеспечение), а НЕ нотионал. Кэшируется."""
        if pid in cls._go_cache:
            return cls._go_cache[pid]
        try:
            from ..st4 import data_feed as _feed
            from ..st4.config import St4Config as _C4
            from .service import ST5_PAIRS as _P
            c = _C4(); c.instruments.asset_ordinary = _P[pid][0]; c.instruments.asset_preferred = _P[pid][1]
            so, sp = _feed.resolve_legs(c)
            go = _feed.leg_margin(so.code) + _feed.leg_margin(sp.code)
        except Exception:  # noqa: BLE001
            go = 0.0
        if go > 0:
            cls._go_cache[pid] = go
        return go

    @classmethod
    def _pos_risk(cls, pid: str, eng: ST5Engine) -> float:
        """РИСК открытой позиции = ГО (обе ноги) × текущие лоты."""
        p = eng.position
        if p is None:
            return 0.0
        return cls.pair_go_per_lot(pid) * p.lots

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
        # движок на каждую пару-кандидата — со СВОИМ конфигом (per-pair оверрайды из ST5_PAIRS)
        self.engines: dict[str, ST5Engine] = {}
        self.pair_cfgs: dict[str, St5Config] = {}
        self.specs: dict[str, tuple] = {}        # pid -> (spec_ord, spec_pref)
        for pid, spec in ST5_PAIRS.items():
            pcfg = self._pair_cfg(pid)
            self.pair_cfgs[pid] = pcfg
            self.engines[pid] = ST5Engine(pid, pcfg, base_lots=pcfg.execution.quantity_lots)
        self.trades: list[dict] = []             # общий журнал портфеля (json-записи)
        self.history: dict[str, list] = {pid: [] for pid in ST5_PAIRS}   # история спреда по парам
        self.events: list[dict] = []
        self.state = {"live": False, "session_started": None, "paused_by_user": False,
                      "data_source": "synthetic", "sandbox_active": False,
                      "real_trading_armed": False}
        self.last_live_ts: dict[str, int] = {pid: 0 for pid in ST5_PAIRS}
        # какие пары торгуем (чекбоксы в UI). По умолчанию все включены.
        self.enabled_pairs: dict[str, bool] = {pid: True for pid in ST5_PAIRS}
        self._lock = asyncio.Lock()
        self._live_task = None
        self._uid_cache: dict[str, tuple] = {}   # pid -> (uid_ord, uid_pref): кэш против 429 T-Bank
        self._legs_cache: dict[str, tuple] = {}  # pid -> (spec_ord, spec_pref) от resolve_legs

    def _pair_cfg(self, pid: str) -> St5Config:
        """Конфиг пары = базовый ST5 + per-pair оверрайды StrategyConfig из ST5_PAIRS[pid][4]."""
        c = St5Config(**self.cfg.model_dump())
        spec = ST5_PAIRS[pid]
        if len(spec) > 4 and isinstance(spec[4], dict):
            for k, v in spec[4].items():
                if hasattr(c.strategy, k):
                    setattr(c.strategy, k, v)
        return c

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
                "enabled": self.enabled_pairs.get(pid, True),
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
            "quantity_lots": self.cfg.execution.quantity_lots,
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
                "enabled_pairs": self.enabled_pairs,
            }
            self._session_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception:  # noqa: BLE001
            pass

    # ---------- боевой контур: взвод реальной торговли ----------
    def arm_real(self, armed: bool) -> None:
        """Взвод реальной торговли (двойной включатель). Сбрасывается при рестарте."""
        self.state["real_trading_armed"] = bool(armed)
        self.log_event("warn" if armed else "info",
                       "🔴 реальная торговля ВЗВЕДЕНА" if armed else "взвод снят")

    def _real_armed(self) -> bool:
        """armed_cb для исполнителя: реальная торговля + cooldown после старта (защита от
        автоордеров на всплеске сразу после live)."""
        if not self.state.get("real_trading_armed"):
            return False
        started = self.state.get("session_started") or 0
        return (time.time() - started) >= 600   # 600с cooldown

    def _audit(self, entry: dict) -> None:
        """Аудит-лог каждого реального/sandbox ордера (неизменяемый журнал для разбора)."""
        self.events.append({"ts": entry["ts"], "kind": "order",
                            "message": f"{entry['op']} {entry['direction']} {entry['lots']}лот "
                                       f"{entry['uid'][:8]} → {entry.get('status')}",
                            "audit": entry})
        if len(self.events) > EVENTS_LEN * 4:   # аудит держим дольше обычных событий
            del self.events[0]

    # ---------- обновление реального капитала (для %-лимитов) ----------
    def refresh_capital(self) -> None:
        """Источник истины капитала для %-лимитов — РЕАЛЬНЫЙ портфель (не paper-баланс)."""
        if not self.state.get("sandbox_active"):
            return
        try:
            from ..st4 import tbank_live as _live
            from ..st4 import tbank_sandbox as _sb
            acc = self.cfg.connector.account_id
            if not acc:
                return
            src = _live if self.cfg.connector.mode == "tbank_real" else _sb
            pf = src.portfolio(acc)
            total = pf.get("totalAmountPortfolio") or pf.get("totalAmountShares")
            if isinstance(total, dict):
                from ..st4.tbank_sandbox import _q_to_float
                total = _q_to_float(total)
            if total and float(total) > 0:
                self.portfolio.capital_rub = float(total)
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
        en = data.get("enabled_pairs") or {}
        self.enabled_pairs = {pid: bool(en.get(pid, True)) for pid in ST5_PAIRS}
        # БЕЗОПАСНОСТЬ: рестарт ВСЕГДА снимает взвод реальной торговли (safe-by-default)
        self.state["real_trading_armed"] = False
        return True

    # ---------- исполнители пар (sandbox/real) ----------
    def _make_executor(self, pid: str):
        """St5PairExecutor для пары, если активен брокерский режим. None → paper (вирт. движок)."""
        if not self.state.get("sandbox_active"):
            return None
        from .executor import St5PairExecutor
        from .service import ST5_PAIRS as _P
        ao, ap = _P[pid][0], _P[pid][1]
        real = self.cfg.connector.mode == "tbank_real"
        # ГОТОВЫЕ uid из кэша (резолвлены по коду СЕРИИ через find_future в _step_pair).
        # Передать asset-коды (TATN) как тикеры нельзя — find_future их не находит → ордер падает.
        uo, up = self._uid_cache.get(pid, (None, None))
        return St5PairExecutor(self.cfg.connector.account_id, ao, ap, real=real,
                               armed_cb=self._real_armed, audit_cb=self._audit,
                               uid_ord=uo, uid_pref=up)

    # ---------- главный live-цикл портфеля ----------
    async def run_live(self) -> None:
        """Live-цикл: тянет свечи по всем парам, прогоняет движки, гейтит входы портфельным
        риском, исполняет (paper/sandbox/real). Один проход на poll_seconds."""
        self.log_event("info", f"ST5 live запущен ({self.cfg.connector.mode}, "
                               f"{len(self.engines)} пар, до {self.cfg.risk.max_open_positions} позиций)")
        warmup_limit = max(self.cfg.strategy.adf_window, self.cfg.strategy.hurst_window) + 60
        replayed = False
        while self.state["live"]:
            try:
                if self.state.get("sandbox_active"):
                    self.refresh_capital()
                async with self._lock:
                    for pid, eng in self.engines.items():
                        if not self.state["live"]:
                            return
                        if not self.enabled_pairs.get(pid, True):
                            continue   # пара выключена чекбоксом — не торгуем
                        await self._step_pair(pid, eng, warmup_limit, replayed)
                        await asyncio.sleep(1.0)   # throttle между парами — против 429 T-Bank
                replayed = True
                self.save_session()
            except Exception as e:  # noqa: BLE001
                self.log_event("warn", f"ST5 live ошибка: {e}")
            await asyncio.sleep(self.cfg.poll_seconds)

    async def _step_pair(self, pid: str, eng, warmup_limit: int, replayed: bool) -> None:
        """Один проход по паре: тянем свежие бары, прогоняем движок, исполняем сделки.

        Источник: в sandbox/real — T-Bank real-time (без лага/обрывов ISS); paper — MOEX ISS.
        """
        from .service import ST5_PAIRS as _P
        ao, ap = _P[pid][0], _P[pid][1]
        from ..st4.config import St4Config as _C4
        c4 = _C4()
        c4.instruments.asset_ordinary = ao
        c4.instruments.asset_preferred = ap
        c4.strategy.candle_interval_minutes = self.cfg.strategy.candle_interval_minutes
        sandbox = self.state.get("sandbox_active", False)
        # после прогрева тянем только хвост (80 баров), не весь warmup — снижает нагрузку/обрывы
        warm = len(eng.spread_buf) < 50
        limit = warmup_limit if warm else 80
        try:
            # резолв серий и uid кэшируем (find_future/resolve_legs дороги и бьют по rate-limit)
            if pid not in self._legs_cache:
                self._legs_cache[pid] = await asyncio.to_thread(feed.resolve_legs, c4)
            so, sp = self._legs_cache[pid]
            if sandbox:
                if pid not in self._uid_cache:
                    from ..st4 import tbank_sandbox as _sb
                    self._uid_cache[pid] = (_sb.find_future(so.code)["uid"],
                                            _sb.find_future(sp.code)["uid"])
                uid_o, uid_p = self._uid_cache[pid]
                df = await asyncio.to_thread(feed.read_ohlcv_tbank, c4, limit, uid_o, uid_p)
            else:
                df = await asyncio.to_thread(feed.read_ohlcv_moex, c4, limit, so.code, sp.code)
                df = df.iloc[:-1]   # ISS: без формирующегося бара
        except Exception as e:  # noqa: BLE001
            self.log_event("warn", f"{pid}: данные недоступны: {e}")
            return
        last_ts = self.last_live_ts.get(pid, 0)
        # «холодный» движок после рестарта: last_ts большой, но spread_buf пуст. Прогреваем ВСЕМИ
        # доступными историческими барами (≤ last_ts) без сделок, чтобы набрать ADF/Hurst-окна.
        cold = len(eng.spread_buf) < min(len(df), self.cfg.strategy.adf_window)
        for ts, row in df.iterrows():
            ts = int(ts)
            if ts <= last_ts:
                if cold:   # прогрев историей до last_ts — БЕЗ открытия позиций (откатываем)
                    eng.step(ts, float(row["price_a"]), float(row["price_b"]))
                    if eng.position is not None and eng.position.bars_held == 0:
                        eng.position = None   # прогревочный «вход» не исполняется в брокере
                continue
            # НОВЫЙ (живой) бар: ts > last_ts. Здесь исполняем реально (это не прогрев).
            pos_before = eng.position is not None
            tr = eng.step(ts, float(row["price_a"]), float(row["price_b"]))
            self.last_live_ts[pid] = ts
            self.push_history(pid, ts)
            # движок ОТКРЫЛ позицию на этом живом баре → портфельный гейт + реальный ордер
            if (not pos_before) and eng.position is not None and eng.position.bars_held == 0:
                self._on_engine_opened(pid, eng, float(row["price_a"]), float(row["price_b"]))
            if tr is not None:
                self._on_engine_trade(pid, eng, tr, float(row["price_a"]), float(row["price_b"]))

    def _on_engine_opened(self, pid: str, eng, ord_px: float, pref_px: float) -> None:
        """Движок открыл позицию (paper). Проверить портфельный гейт; в брокере — реальный ордер.
        Если гейт не пропустил — откатить позицию движка (вход не состоялся)."""
        from .service import ST5_PAIRS as _P
        issuer = _P[pid][2]
        p = eng.position
        risk = self.portfolio.pair_go_per_lot(pid) * p.lots   # риск = ГО позиции (не нотионал)
        ok, reason = self.portfolio.can_open(pid, issuer, risk, self.engines, _P)
        if not ok:
            eng.position = None   # вход запрещён портфелем → откат
            self.log_event("info", f"{pid}: вход отклонён ({reason})")
            return
        ex = self._make_executor(pid)
        if ex is not None:
            try:
                long_spread = (p.state == St5State.LONG_SPREAD)
                ex.open_pair(long_spread, p.lots, ord_px, pref_px)
                self.portfolio.on_success()
            except Exception as e:  # noqa: BLE001
                eng.position = None
                self.portfolio.on_error()
                self.portfolio.halt_pair(pid, f"вход не исполнен: {e}")
                self.log_event("warn", f"{pid}: вход в брокере не удался: {e}")
                return
        self.log_event("position", f"{pid}: вход {p.state.value} z={p.entry_z:+.2f} lots={p.lots}")

    def _on_engine_trade(self, pid: str, eng, tr, ord_px: float, pref_px: float) -> None:
        """Движок закрыл (полностью/частично). В брокере — реальный закрывающий ордер."""
        ex = self._make_executor(pid)
        if ex is not None:
            try:
                long_spread = (tr.state == St5State.LONG_SPREAD)
                op = "take50" if tr.reason == "take_partial" else "flat"
                ex.close_pair(long_spread, tr.lots, ord_px, pref_px, op=op)
                self.portfolio.on_success()
            except Exception as e:  # noqa: BLE001
                self.portfolio.on_error()
                self.portfolio.halt_pair(pid, f"выход не исполнен: {e}")
                self.log_event("warn", f"{pid}: выход в брокере не удался: {e}")
        self.portfolio.on_trade(tr.net_pnl_rub)
        rec = {"pair": pid, "state": tr.state.value, "entry_ts": tr.entry_ts, "exit_ts": tr.exit_ts,
               "entry_z": tr.entry_z, "exit_z": tr.exit_z, "lots": tr.lots,
               "net_pnl_rub": tr.net_pnl_rub, "reason": tr.reason, "bars_held": tr.bars_held}
        self.trades.append(rec)
        self.log_event("exit", f"{pid}: {tr.reason} net {tr.net_pnl_rub:+.0f}₽ ({tr.lots}лот)")
