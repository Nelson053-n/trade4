"""RiskManager — лимиты, kill-switch, дневной P&L (§11).

Не исполняет ордера; только разрешает/запрещает входы и переводит в HALTED при
накоплении ошибок. Дневной убыток считается по реализованным сделкам в TZ сессии.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import RiskConfig, SessionConfig


class RiskManager:
    def __init__(self, risk: RiskConfig, session: SessionConfig) -> None:
        self.cfg = risk
        self.session = session
        self.consecutive_errors = 0
        self._day: str = ""           # текущий торговый день (YYYY-MM-DD в TZ сессии)
        self.day_pnl_rub = 0.0        # реализованный P&L за день
        self.halted = False
        self.halt_reason = ""

    def _day_key(self, ts_ms: int) -> str:
        try:
            local = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(
                ZoneInfo(self.session.timezone))
        except Exception:  # noqa: BLE001
            local = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        return local.strftime("%Y-%m-%d")

    def on_trade_closed(self, net_pnl_rub: float, ts_ms: int) -> None:
        """Учесть закрытую сделку в дневном P&L (с переходом дня)."""
        day = self._day_key(ts_ms)
        if day != self._day:
            self._day = day
            self.day_pnl_rub = 0.0
        self.day_pnl_rub += net_pnl_rub

    def day_loss_breached(self, ts_ms: int, unrealized_rub: float = 0.0) -> bool:
        """Дневной лимит убытка с учётом НЕРЕАЛИЗОВАННОГО (§11): открытая позиция
        может превысить лимит, не закрыв ни одной сделки — реализованный day_pnl
        этого не видит. Прибыльный unrealized лимит не ослабляет (только min(0, ·))."""
        realized = self.day_pnl_rub if self._day_key(ts_ms) == self._day else 0.0
        return realized + min(0.0, unrealized_rub) <= -self.cfg.max_daily_loss_rub

    def can_enter(self, ts_ms: int, open_positions: int) -> tuple[bool, str]:
        """Разрешён ли новый вход сейчас (§11)."""
        if self.halted:
            return False, f"HALTED: {self.halt_reason}"
        if not self.cfg.trading_enabled:
            return False, "торговля выключена оператором"
        if open_positions >= self.cfg.max_open_positions:
            return False, "достигнут лимит открытых позиций"
        # дневной лимит убытка: сравниваем по текущему дню
        if self._day_key(ts_ms) == self._day and self.day_pnl_rub <= -self.cfg.max_daily_loss_rub:
            return False, f"дневной лимит убытка {self.cfg.max_daily_loss_rub:.0f}₽"
        return True, ""

    def on_error(self) -> None:
        """Ошибка коннектора/реджект — инкремент серии; при превышении → HALTED."""
        self.consecutive_errors += 1
        if self.consecutive_errors >= self.cfg.max_consecutive_errors:
            self.halt(f"серия ошибок ≥ {self.cfg.max_consecutive_errors}")

    def on_success(self) -> None:
        self.consecutive_errors = 0

    def halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason

    def resume(self) -> None:
        """Ручной разбор завершён — снять HALTED (только оператором)."""
        self.halted = False
        self.halt_reason = ""
        self.consecutive_errors = 0
