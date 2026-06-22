"""Конфигурация st4 (арбитраж спреда SBRF/SBPR) — pydantic v2, без хардкода.

Параметры разложены по разделам ТЗ: инструменты/роллировер, стратегия (BB),
исполнение (paper), хедж, риск, сессия. Значения по умолчанию — рабочие для
ближайших ликвидных серий FORTS; коды серий обновляются авто-роллировером.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class InstrumentsConfig(BaseModel):
    """Ноги пары и параметры роллировера квартальных контрактов (§5, §6.4)."""
    contract_type: Literal["Quarterly", "Perpetual"] = "Quarterly"
    asset_ordinary: str = "SBRF"          # обыкновенные — серии SR*
    asset_preferred: str = "SBPR"         # привилегированные — серии SP*
    # явные коды серий; при auto_rollover пустые → подбираются по справочнику ISS
    leg_ordinary_code: str = ""           # напр. SRM6
    leg_preferred_code: str = ""          # напр. SPM6
    auto_rollover: bool = True
    rollover_days_before_expiry: int = 3  # за сколько дней до экспирации роллить/не входить
    # окно «тишины» перед экспирацией: новые входы стопаем за столько дней. Держим = roll-окну,
    # чтобы не было «мёртвого коридора» (входов нет, но и роллировера ещё нет): как только
    # d2e < 3 — позиции закрываются и серия роллится на следующую.
    rollover_no_new_entry_days_before: int = 3


class StrategyConfig(BaseModel):
    """Сигнальная логика: Bollinger Bands на спреде SBPR−SBRF (§8, §9)."""
    candle_interval_minutes: int = 10     # MOEX ISS: 5m нет, дефолт 10m нативные
    sma_period: int = 200
    sigma_multiplier: float = 2.0
    std_mode: Literal["Population", "Sample"] = "Population"   # /N или /(N−1)
    # гейт отклонения от средней. AbsOfMean знаконезависим (корректно при любом знаке
    # спреда), LiteralPct — буквально из исходного ТЗ (ломается при SMA<0).
    # Sigma — порог в единицах σ спреда (рекомендуется: %|SMA| у спреда, близкого к 0,
    # вырождается — σ масштабируется с волатильностью всегда).
    deviation_mode: Literal["AbsOfMean", "LiteralPct", "Sigma"] = "AbsOfMean"
    deviation_pct: float = 0.02           # 2% (для AbsOfMean/LiteralPct)
    deviation_sigma: float = 2.2          # для Sigma: |spread−SMA| ≥ deviation_sigma·σ
    # триггер входа: Breakout — первый пробой полосы наружу (ловит нож при структурном
    # сдвиге); ReEntry — вход по ВОЗВРАТУ в канал (спред был снаружи и вернулся внутрь)
    entry_trigger: Literal["Breakout", "ReEntry"] = "Breakout"
    # выход по пересечению средней: живая SMA (дрейфует) или зафиксированная на входе
    freeze_sma_on_exit: bool = False
    max_bars_in_trade: int = 0            # тайм-стоп (0 = выключен)
    # ручной режим: через сколько закрытых баров неподтверждённая рекомендация
    # протухает (авто-reject) — иначе pending блокирует подачу баров бесконечно,
    # а approve исполнил бы по давно устаревшей цене
    pending_ttl_bars: int = 3
    # ЗАЩИТНЫЙ СТОП (расширение сверх §9.4). При stop_sigma>0: если спред ушёл против
    # позиции дальше stop_sigma·σ от средней — закрываем по стопу. Защита от разрыва
    # коинтеграции/тренда, иначе позиция может висеть бесконечно. Дефолт 4.0 — по гриду
    # на 60д MOEX (12.06.2026): 3σ даёт 10 ложных стопов (−20% net), 4σ — 1 стоп и
    # net −2% к безстоповому, 5σ не срабатывает (страховка «бесплатна», но реальна).
    stop_sigma: float = 4.0
    # --- доработки входа/выхода (2026-06-14) ---
    # тейк-профит: закрыть позицию, когда спред вернулся к средней на take_profit_sigma·σ
    # ВНУТРИ канала (не дожидаясь полного пересечения SMA). 0 = выключен (выход только по SMA).
    take_profit_sigma: float = 0.0
    # объёмный фильтр входа: пробой полосы принимается, только если объём бара (сумма
    # объёмов обеих ног) ≥ volume_filter_mult · SMA(объёма, sma_period). 0 = выключен.
    volume_filter_mult: float = 0.0
    # гейт свежести данных: не входить, если последний бар старше max_data_lag_min минут
    # (защита от входа по устаревшей цене в ISS-режиме с задержкой свечей). 0 = выключен.
    max_data_lag_min: float = 0.0


class ExecutionConfig(BaseModel):
    """Параметры исполнения парного ордера (paper-модель §10)."""
    entry_style: Literal["MarketableLimit", "Passive"] = "MarketableLimit"
    tick_offset: int = 1                  # ± N тиков от лучшего бид/аск
    order_timeout_seconds: int = 2
    max_retries: int = 3
    first_leg_to_fill: Literal["Preferred", "Ordinary"] = "Preferred"  # менее ликвидную первой
    deviation_protection_ticks: int = 5   # макс. отклонение цены → отмена входа
    quantity_lots: int = 1                # лотов на ногу (для β=1)
    # полуширина paper-стакана в тиках: книга = close ± halfspread·tick. Реальный стакан
    # SBPR заметно шире одного тика — занижение этого параметра приукрашивает paper-P&L
    paper_book_halfspread_ticks: float = 1.0
    # paper-модель неисполнения: вероятность, что нога не зальётся за max_retries
    # (для тестов атомарности/unwind; 0 = всегда заливается — детерминированный paper)
    paper_fill_fail_prob: float = 0.0


class HedgeConfig(BaseModel):
    """Хедж-коэффициент β (отношение лотов SBPR:SBRF), §9.5."""
    beta: float = 1.0
    use_dynamic_beta: bool = False        # одна точка истины EntryBeta при включении


class RiskConfig(BaseModel):
    """Лимиты и kill-switch (§11)."""
    max_open_positions: int = 1
    max_daily_loss_rub: float = 50_000.0
    max_consecutive_errors: int = 5
    trading_enabled: bool = True


class SessionConfig(BaseModel):
    """Торговая сессия FORTS (§9.7)."""
    timezone: str = "Europe/Moscow"
    skip_clearing_windows: bool = True
    # окна клиринга/аукционов МосБиржи (мин. от полуночи MSK), [начало, конец)
    clearing_windows: list[tuple[int, int]] = Field(
        default_factory=lambda: [(14 * 60, 14 * 60 + 5), (18 * 60 + 45, 19 * 60 + 5)]
    )


class Paper(BaseModel):
    """Виртуальный счёт (paper)."""
    start_balance_rub: float = 1_000_000.0
    taker_fee_rub_per_lot: float = 2.0    # биржевой+брокерский сбор за лот за ногу (оценка)


class ConnectorConfig(BaseModel):
    """Выбор исполнителя ордеров (§14.3). Только paper или sandbox — боевой контур запрещён.

    ВНИМАНИЕ: токена здесь НЕТ — секрет не сериализуется в session_state_4.json. Токен T-Bank
    живёт только в окружении процесса (env TBANK_TOKEN). account_id/payin_rub несекретны и
    переживают рестарт. Sandbox-режим активен только в live (на синтетике трактуется как paper).
    """
    mode: Literal["paper", "tbank_sandbox"] = "paper"
    account_id: str = ""                  # переиспользуемый sandbox-счёт; пусто → открыть новый
    payin_rub: int = 200_000              # пополнение sandbox-счёта под ГО при старте
    account_name: str = "st4-spread-sandbox"


class St4Config(BaseModel):
    instruments: InstrumentsConfig = Field(default_factory=InstrumentsConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    hedge: HedgeConfig = Field(default_factory=HedgeConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    paper: Paper = Field(default_factory=Paper)
    connector: ConnectorConfig = Field(default_factory=ConnectorConfig)
    poll_seconds: int = 20                # период опроса ISS в live-режиме
    auto_approve: bool = True             # вход авто (FSM безостановочный) | human-in-the-loop
