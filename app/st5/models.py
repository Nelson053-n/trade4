"""Модели ST5 — позиция с β, частичной фиксацией, half-life и z-метаданными.

Отдельно от st4.models (там позиция под 1 пару без частичных выходов). Переиспользуем
Role/LegPosition из st4. ST5 держит до 3 позиций одновременно (управление — в портфеле/движке).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class St5State(str, Enum):
    FLAT = "flat"
    LONG_SPREAD = "long_spread"      # spread дёшев (z<0): buy pref + sell ord, ставка на рост
    SHORT_SPREAD = "short_spread"    # spread дорог (z>0): sell pref + buy ord, ставка на падение


@dataclass
class St5Position:
    """Открытая позиция ST5 по одной паре. Поддерживает частичную фиксацию (lots уменьшается)."""
    pair: str
    state: St5State
    entry_ts: int
    entry_z: float
    entry_spread: float
    entry_beta: float                # β на входе (для размеров ног и P&L)
    lots: int                        # ТЕКУЩИЙ размер (после частичных фиксаций)
    entry_lots: int                  # исходный размер (для метрик)
    ord_entry: float                 # цена обычки на входе
    pref_entry: float                # цена префа на входе
    half_life: float                 # на входе (для time-stop)
    bars_held: int = 0
    partial_done: bool = False       # сделана ли частичная фиксация 50%
    fees_rub: float = 0.0            # накопленные комиссии (вход + частичные)
    realized_rub: float = 0.0        # реализованный P&L от частичных фиксаций

    def notional(self) -> float:
        """Грубый нотионал позиции в пунктах (для %-лимитов): |pref| + |β·ord|."""
        return abs(self.pref_entry) + abs(self.entry_beta * self.ord_entry)


@dataclass
class St5Trade:
    """Закрытая (полностью или частично) сделка ST5 — запись в журнал."""
    pair: str
    state: St5State
    entry_ts: int
    exit_ts: int
    entry_z: float
    exit_z: float
    entry_spread: float
    exit_spread: float
    lots: int
    gross_pnl_rub: float
    fees_rub: float
    net_pnl_rub: float
    reason: str                      # "take_partial" | "exit" | "z_stop" | "time_stop" | "adf_break" | "flat_all"
    bars_held: int = 0
    entry_beta: float = 1.0


@dataclass
class FilterState:
    """Снимок рыночных фильтров (кэшируется, пересчёт раз в N баров)."""
    adf_p: float = 1.0
    hurst: float = 0.5
    half_life: float = float("inf")
    rv_ratio: float = 0.0
    cointegrated: bool = False       # ADF p < порог
    mean_reverting: bool = False     # Hurst в окне
    calm_regime: bool = False        # RV ratio < порог
    bars_since_calc: int = 0

    def entry_allowed(self) -> bool:
        """Все рыночные фильтры пройдены — вход в НОВУЮ позицию разрешён."""
        return self.cointegrated and self.mean_reverting and self.calm_regime


@dataclass
class St5Metrics:
    """Метрики бэктеста ST5."""
    trades: int = 0
    wins: int = 0
    net_pnl_rub: float = 0.0
    gross_pnl_rub: float = 0.0
    fees_rub: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0          # как Sharpe, но по downside-волатильности
    calmar: float = 0.0           # net / maxDD (доход на единицу просадки)
    profit_factor: float = 0.0
    expectancy: float = 0.0       # средний P&L на сделку
    avg_bars_held: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    bars: int = 0
    reasons: dict = field(default_factory=dict)
    equity_curve: list = field(default_factory=list)

    @property
    def win_rate_pct(self) -> float:
        return (self.wins / self.trades * 100.0) if self.trades else 0.0
