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

    # модель издержек ЖУРНАЛА в live: полспреда стакана на ногу, в пунктах цены. Реальные
    # маркет-филлы платят спред, которого нет в клоузах баров — без этого журнал оптимистичнее
    # счёта (инцидент-разбор 02.07: ~2.7к₽/день скрытых издержек). На сигналы НЕ влияет.
    half_spread_pts: float = 2.0

    # комиссия брокера: ДОЛЯ ОТ НОТИОНАЛА операции (сверка леджера 03.07: песочница берёт
    # ровно 0.05% с каждой операции — фикс ₽/лот занижал комиссию в ~5.5×, «исполнение ±»
    # копил минус). 0 → старая фикс-модель fee_per_lot (совместимость). Боевой тариф
    # T-Bank уточнить ДО реала — может отличаться от песочного.
    fee_rate: float = 0.0005

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
    # кап числа β-юнитов позиции (0 = без лимита). Для пар с крупным юнитом по тонкой ноге
    # (tatn: юнит = 10 префов TPU6 при медиане 23 конт/10м-бар) тиры ×2 съедали бы стакан.
    max_units: int = 0

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
    """Портфельный риск ST5. Лимиты ГО — ФИКСИРОВАННЫЕ в ₽ (не % капитала): ГО фьючерса —
    абсолютная величина (залог биржи), не зависит от размера счёта, и %-привязка ломалась бы
    на малом боевом счёте (0.5% от 100к = 500₽ < ГО любой пары → торговля заморожена)."""
    # ₽-лимиты ГО (приоритетные). 0 → выкл, fallback на %-капитала ниже (legacy/совместимость).
    # Подобраны под боевой счёт ~200к: реальное ГО (ISS×go_factor≈2.69) 1 лот/пара ≈ 58-68к,
    # все 3 пары ×1 лот ≈ 94к (47% от 200к, есть буфер на вариационную маржу).
    max_go_per_trade_rub: float = 80_000.0    # потолок ГО одной сделки (1 лот любой пары проходит)
    max_go_portfolio_rub: float = 150_000.0   # потолок суммарного ГО (3 пары ×1 лот + буфер)
    risk_per_trade_pct: float = 0.005        # 0.5% капитала на сделку (legacy fallback)
    risk_per_pair_pct: float = 0.015         # 1.5% на пару
    risk_per_portfolio_pct: float = 0.05     # 5% на портфель (legacy fallback)
    max_open_positions: int = 3              # макс. одновременно открытых позиций
    max_per_issuer: int = 1                  # не более 1 позиции на эмитента
    max_daily_loss_rub: float = 50_000.0
    max_consecutive_errors: int = 3
    trading_enabled: bool = True


class St5NotifyConfig(BaseModel):
    """Telegram-уведомления (только исходящие). Токен бота — НЕ здесь (env/файл .tg_bot_token)."""
    enabled: bool = False
    chat_id: str = ""                        # ID чата/пользователя для отправки
    notify_entry: bool = True                # вход в позицию
    notify_exit: bool = True                 # выход/частичная фиксация (с P&L)
    notify_errors: bool = True               # ошибки исполнения/коннектора
    notify_before_open: bool = True          # напоминание за before_open_min до открытия биржи
    before_open_min: int = 10
    daily_summary: bool = True               # дневная сводка при закрытии вечерней сессии
    notify_reconcile: bool = True            # расхождение ног движок↔счёт (периодическая сверка)
    notify_missed: bool = False              # упущенные входы (сигнал был, вход отклонён) — шумно


class St5Config(BaseModel):
    """Полный конфиг ST5: стратегия + портфельный риск + общая инфраструктура (исполнение/коннектор)."""
    instruments: InstrumentsConfig = InstrumentsConfig()
    strategy: St5StrategyConfig = St5StrategyConfig()
    risk: St5RiskConfig = St5RiskConfig()
    execution: ExecutionConfig = ExecutionConfig()
    session: SessionConfig = SessionConfig()
    paper: Paper = Paper()
    connector: ConnectorConfig = ConnectorConfig()
    notify: St5NotifyConfig = St5NotifyConfig()
    auto_approve: bool = True                # statarb — авто-исполнение (ручной approve не нужен)
    poll_seconds: float = 15.0               # бар раз в 10 мин → частый опрос не нужен (+rate-limit)
    # Счёт ВЫДЕЛЕН под st5 (после развода счетов 02.07): чужих движков на нём нет, значит
    # ноги при flat-движке = НАШ обрыв (сорванный unwind и т.п.) → periodic reconcile
    # АВТОЗАКРЫВАЕТ их маркетом + тревога. False — старое поведение (общий счёт, не трогать).
    dedicated_account: bool = True
