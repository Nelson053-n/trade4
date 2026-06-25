"""Конфигурация ST5 — institutional statistical arbitrage (коинтеграция + Kalman β + z-score).

Все параметры стратегии из ТЗ ST5. Переиспользует RiskConfig/ExecutionConfig/ConnectorConfig
из st4 (общая инфраструктура T-Bank/исполнения), добавляет St5StrategyConfig и портфельные лимиты.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# переиспускаем общие конфиги исполнения/коннектора/риска/сессии из st4 (инфраструктура общая)
from ..st4.config import (
    ConnectorConfig,
    ExecutionConfig,
    InstrumentsConfig,
    Paper,
    SessionConfig,
)


class St5StrategyConfig(BaseModel):
    """Сигнальная логика ST5: spread = pref − β·ord, z-score, фильтры коинтеграции/Hurst/режима."""
    candle_interval_minutes: int = 10        # торговый ТФ

    # --- hedge ratio β ---
    beta_method: Literal["kalman", "rolling_ols"] = "kalman"
    ols_window: int = 250                    # окно Rolling OLS (если beta_method=rolling_ols)
    kalman_delta: float = 1e-4               # process noise: Q = δ/(1−δ)·I (меньше → β инертнее)
    kalman_obs_noise: float = 1e-3           # R — observation noise (шум спреда)
    kalman_warmup: int = 50                  # баров на сходимость β; их спред НЕ кормим фильтрам

    # --- z-score сигнал ---
    z_ema_span: int = 150                    # EMA(150) для μ спреда
    z_std_window: int = 150                  # rolling_std(150) для σ

    # --- фильтры рынка (gate, пересчёт раз в filter_recalc_bars баров) ---
    adf_window: int = 500                    # окно rolling ADF
    adf_p_enter: float = 0.05                # торговля разрешена при ADF p < 0.05
    adf_p_break: float = 0.15                # structural break stop: закрыть при ADF p > 0.15
    hurst_window: int = 500                  # окно Hurst
    hurst_min: float = 0.15                  # mean-reverting фильтр
    # ВНИМАНИЕ: R/S систематически ЗАВЫШАЕТ Hurst на финансовых рядах (известное смещение).
    # ТЗ задаёт 0.45, но по бэктесту реально mean-reverting пары (sngr) дают H≈0.55 по R/S и
    # отсекаются → 0 сделок. Поднято до 0.60 — sngr/tatn торгуют, sber (H 0.33-0.45) не задет.
    hurst_max: float = 0.60
    rv_short: int = 20                       # RV20
    rv_long: int = 100                       # RV100
    rv_ratio_max: float = 1.7                # вход при RV20/RV100 < 1.7
    filter_recalc_bars: int = 10             # дорогие фильтры (ADF/Hurst/HL) пересчитываем раз в N баров

    # --- вход ---
    z_entry: float = 2.25                    # |z| > 2.25 для входа
    # Δz-подтверждение (вход только на схождении z к нулю). По бэктесту MOEX оказался слишком
    # строгим — на стабильном спреде z редко разворачивается ровно на баре пробоя порога → 0 сделок.
    # Выключен по умолчанию; вход по чистому |z|>порог даёт sber Sharpe 3-6 в OOS-сплите.
    require_dz_confirm: bool = False
    z_no_entry: float = 4.0                  # |z| > 4 → вход запрещён (слишком далеко, риск разрыва)

    # --- сайзинг по |z| (множители к базовому размеру) ---
    # |z| 2.25–2.75 → 1x; 2.75–3.25 → 1.5x; 3.25–4.0 → 2x; >4 запрещён
    size_tiers: list[tuple[float, float, float]] = [
        (2.25, 2.75, 1.0),
        (2.75, 3.25, 1.5),
        (3.25, 4.00, 2.0),
    ]

    # --- стопы ---
    z_stop: float = 4.25                     # stop-loss: |z| > 4.25
    half_life_stop_mult: float = 3.0         # time-stop: max_hold = 3 × half_life (в барах)

    # --- выход (частичная фиксация) ---
    z_take_partial: float = 1.0              # фиксируем 50% при |z| < 1
    partial_take_frac: float = 0.5
    z_exit_full: float = 0.35                # остаток при |z| < 0.35

    # --- временные ограничения (минуты от границ сессии, не торговать) ---
    no_entry_open_min: int = 15              # первые 15 мин торгов
    no_entry_before_clearing_min: int = 15   # за 15 мин до клиринга
    no_entry_after_clearing_min: int = 10    # 10 мин после клиринга
    no_entry_before_close_min: int = 20      # последние 20 мин вечерней сессии


class St5RiskConfig(BaseModel):
    """Портфельный риск ST5 (% от РЕАЛЬНОГО капитала портфеля)."""
    risk_per_trade_pct: float = 0.005        # 0.5% капитала на сделку
    risk_per_pair_pct: float = 0.015         # 1.5% на пару
    risk_per_portfolio_pct: float = 0.05     # 5% на портфель
    max_open_positions: int = 3              # макс. одновременно открытых позиций
    max_per_issuer: int = 1                  # не более 1 позиции на эмитента
    max_daily_loss_rub: float = 50_000.0
    max_consecutive_errors: int = 3
    trading_enabled: bool = True


class St5Config(BaseModel):
    """Полный конфиг ST5: стратегия + портфельный риск + общая инфраструктура (исполнение/коннектор)."""
    instruments: InstrumentsConfig = InstrumentsConfig()
    strategy: St5StrategyConfig = St5StrategyConfig()
    risk: St5RiskConfig = St5RiskConfig()
    execution: ExecutionConfig = ExecutionConfig()
    session: SessionConfig = SessionConfig()
    paper: Paper = Paper()
    connector: ConnectorConfig = ConnectorConfig()
    auto_approve: bool = True                # statarb — авто-исполнение (ручной approve не нужен)
    poll_seconds: float = 15.0               # бар раз в 10 мин → частый опрос не нужен (+rate-limit)
