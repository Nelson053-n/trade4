"""Сигнальная логика st4 (§9): пробой полос + гейт отклонения + выход к средней.

Чистая функция от (prev, cur, sma) → сигнал. Не знает про позиции/исполнение — только
правила входа/выхода. FSM и исполнение живут в engine.py. Сигналы оцениваются ТОЛЬКО на
закрытии спред-бара и только когда BB.is_ready.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import SessionConfig, StrategyConfig
from .models import BandReading, BotState, Signal


def deviation_gate(side: Signal, cur: float, sma: float, cfg: StrategyConfig,
                   sigma: float = 0.0) -> bool:
    """Гейт отклонения от средней (§9.3). Возвращает True, если порог пройден.

    AbsOfMean (по умолчанию, корректно при любом знаке SMA):
        SELL: cur − SMA >= DeviationPct·|SMA|
    LiteralPct (буквально из исходного ТЗ, ломается при SMA<0):
        SELL: cur >= SMA·(1 + DeviationPct)
    Sigma (порог в σ спреда — не вырождается при SMA→0):
        SELL: cur − SMA >= DeviationSigma·σ
    BUY — зеркально.
    """
    p = cfg.deviation_pct
    if cfg.deviation_mode == "Sigma":
        thr = cfg.deviation_sigma * sigma
        return (cur - sma) >= thr if side == Signal.SELL else (sma - cur) >= thr
    if cfg.deviation_mode == "AbsOfMean":
        thr = p * abs(sma)
        return (cur - sma) >= thr if side == Signal.SELL else (sma - cur) >= thr
    # LiteralPct
    if side == Signal.SELL:
        return cur >= sma * (1.0 + p)
    return cur <= sma * (1.0 - p)


def entry_signal(prev: BandReading, cur: BandReading, cfg: StrategyConfig) -> Signal:
    """Сигнал входа: пересечение полосы (§9.2) И гейт отклонения (§9.3).

    Breakout (по умолчанию): пробой полосы НАРУЖУ.
      SELL: prev < Upper и cur >= Upper;  BUY: prev > Lower и cur <= Lower.
    ReEntry: ВОЗВРАТ в канал (спред был снаружи и вернулся внутрь) — позже, но
      защищён от входа в начале структурного сдвига. Гейт отклонения проверяем
      по prev (экстремум): cur уже внутри канала и порог в σ не прошёл бы никогда.
    """
    if not (cur.is_ready and prev.is_ready):
        return Signal.NONE
    if cfg.entry_trigger == "ReEntry":
        if prev.spread >= prev.upper and cur.spread < cur.upper:
            if deviation_gate(Signal.SELL, prev.spread, prev.sma, cfg, prev.sigma):
                return Signal.SELL
        if prev.spread <= prev.lower and cur.spread > cur.lower:
            if deviation_gate(Signal.BUY, prev.spread, prev.sma, cfg, prev.sigma):
                return Signal.BUY
        return Signal.NONE
    # Breakout: пробой верхней полосы → SELL
    if prev.spread < prev.upper and cur.spread >= cur.upper:
        if deviation_gate(Signal.SELL, cur.spread, cur.sma, cfg, cur.sigma):
            return Signal.SELL
    # пробой нижней полосы → BUY
    if prev.spread > prev.lower and cur.spread <= cur.lower:
        if deviation_gate(Signal.BUY, cur.spread, cur.sma, cfg, cur.sigma):
            return Signal.BUY
    return Signal.NONE


def exit_signal(state: BotState, prev: BandReading, cur: BandReading,
                sma_level: float) -> bool:
    """Выход по пересечению средней (§9.4). sma_level — живая SMA или зафиксированная.

    SHORT_SPREAD: спред пересекает среднюю сверху вниз (prev > SMA и cur <= SMA).
    LONG_SPREAD:  снизу вверх (prev < SMA и cur >= SMA).
    """
    if state == BotState.SHORT_SPREAD:
        return prev.spread > sma_level and cur.spread <= sma_level
    if state == BotState.LONG_SPREAD:
        return prev.spread < sma_level and cur.spread >= sma_level
    return False


def in_clearing_window(ts_ms: int, cfg: SessionConfig) -> bool:
    """Попадает ли момент в клиринговое окно/аукцион (§9.7). Время — в TZ сессии."""
    if not cfg.skip_clearing_windows:
        return False
    try:
        local = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(
            ZoneInfo(cfg.timezone))
    except Exception:  # noqa: BLE001  отсутствие tzdata не должно ронять торговлю
        return False
    minutes = local.hour * 60 + local.minute
    return any(lo <= minutes < hi for lo, hi in cfg.clearing_windows)


def is_nan(x: float) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))
