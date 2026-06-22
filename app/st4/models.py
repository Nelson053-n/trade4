"""Доменные модели st4: enum'ы и dataclass'ы. Только структуры данных.

Терминология из ТЗ (§2): нога (leg), спред = Close(SBPR) − Close(SBRF),
шорт спреда = продать SBRF + купить SBPR, лонг = наоборот.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class BotState(str, Enum):
    """Конечный автомат TradingEngine (§9.1)."""
    FLAT = "flat"
    ENTERING_SHORT = "entering_short"
    ENTERING_LONG = "entering_long"
    SHORT_SPREAD = "short_spread"   # продали SBRF + купили SBPR (ставка на сужение спреда)
    LONG_SPREAD = "long_spread"     # купили SBRF + продали SBPR (ставка на рост спреда)
    EXITING = "exiting"
    HALTED = "halted"               # авария — только ручной разбор


class Signal(str, Enum):
    NONE = "none"
    SELL = "sell"   # шорт спреда: пробой верхней полосы + гейт
    BUY = "buy"     # лонг спреда: пробой нижней полосы + гейт
    EXIT = "exit"   # пересечение средней


class Role(str, Enum):
    ORDINARY = "SBRF"
    PREFERRED = "SBPR"


@dataclass(slots=True)
class InstrumentSpec:
    """Справочник инструмента (§6): тик, стоимость шага, лот, экспирация."""
    code: str                  # SECID серии, напр. SRM6
    role: Role
    tick_size: float           # MINSTEP — минимальный шаг цены (пункты)
    tick_value_rub: float      # STEPPRICE — рублёвая стоимость одного шага
    lot: int                   # LOTSIZE
    expiry: Optional[str] = None   # LASTTRADEDATE 'YYYY-MM-DD'


@dataclass(slots=True)
class Candle:
    """Закрытая свеча (§6). open_time — UTC unix ms."""
    code: str
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool = True


@dataclass(slots=True)
class SpreadBar:
    """Свеча спреда: SpreadClose(t) = Close(SBPR,t) − Close(SBRF,t) (§7)."""
    ts: int                    # open_time бара (общий для обеих ног)
    close_ord: float           # Close(SBRF)
    close_pref: float          # Close(SBPR)
    spread: float              # close_pref − close_ord
    volume: float = 0.0        # сумма объёмов обеих ног (для объёмного фильтра входа)


@dataclass(slots=True)
class BandReading:
    """Срез Bollinger на закрытый спред-бар (§8.2)."""
    ts: int
    spread: float
    sma: float
    sigma: float
    upper: float
    lower: float
    is_ready: bool


@dataclass(slots=True)
class OrderBookSnapshot:
    """Лучшие бид/аск ноги (для расчёта лимитной цены, §10.1)."""
    code: str
    best_bid: float
    best_ask: float


@dataclass(slots=True)
class Fill:
    """Результат исполнения ноги (paper)."""
    code: str
    role: Role
    side: str                  # "buy" | "sell"
    lots: int
    avg_price: float           # средняя цена исполнения (в пунктах)
    reference_price: float     # расчётная цена-ориентир (для проскальзывания)
    slippage_ticks: float      # (avg − reference)/tick со знаком против нас
    retries: int = 0


@dataclass(slots=True)
class LegPosition:
    code: str
    role: Role
    side: str                  # "buy"(long) | "sell"(short)
    lots: int
    entry_price: float         # средняя цена входа (пункты)


@dataclass(slots=True)
class Position:
    """Открытая парная позиция (одна точка истины EntryBeta, §9.5)."""
    state: BotState            # SHORT_SPREAD | LONG_SPREAD
    leg_ord: LegPosition
    leg_pref: LegPosition
    entry_ts: int
    entry_spread: float
    entry_beta: float          # β, зафиксированная на входе — для P&L и размеров ног
    sma_at_entry: float        # для FreezeSmaOnExit
    entry_fee_rub: float = 0.0


@dataclass(slots=True)
class Trade:
    """Закрытая парная сделка (журнал §13)."""
    state: BotState            # направление: SHORT_SPREAD | LONG_SPREAD
    entry_ts: int
    exit_ts: int
    entry_spread: float
    exit_spread: float
    lots: int
    gross_pnl_rub: float
    fees_rub: float
    net_pnl_rub: float
    reason: str                # "exit" | "stop" | "flat_all" | "time_stop"
    bars_held: int = 0
    # цены исполнения ног (вход/выход) — «по чём купил/продал»
    ord_side: str = ""
    pref_side: str = ""
    ord_entry: float = 0.0
    ord_exit: float = 0.0
    pref_entry: float = 0.0
    pref_exit: float = 0.0
    # суммарное проскальзывание входа+выхода, тики
    slippage_ticks: float = 0.0


@dataclass(slots=True)
class EngineEvent:
    """Событие движка для журнала/WS (§12.2)."""
    ts: int
    kind: str                  # signal | position | order | exit | halt | warn | info
    message: str
    data: dict = field(default_factory=dict)
