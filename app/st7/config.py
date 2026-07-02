"""Конфигурация ST7 — «фандинг-давление»: полухеджированный шорт вечного фьюча.

Гипотеза (подтверждена бэктестом 02.07.2026 на всей истории перпов): аномально высокий
фандинг = толпа в лонгах с плечом = перегрев → форвардная слабость. Позиция: ШОРТ вечного
(2×паритет) + ЛОНГ квартальника (1×) — полухедж: половина направленного риска снята,
фандинг собирается с ПОЛНОГО шорта. Бэктест (издержки учтены): IMOEXF +37.7% нотионала
за 2.6 года (maxDD 10.7%), GAZPF +41.6% за 1.75 года (maxDD 11.9%), прибыль в каждом
полном году; SBERF сигнал не работает — пара выключена.
"""
from __future__ import annotations

from pydantic import BaseModel


class St7StrategyConfig(BaseModel):
    """Сигнальная логика ST7 (дневная гранулярность, решение после вечернего клиринга)."""
    fund_enter_pp: float = 35.0       # вход: 3д-средний аннуализ. фандинг > 35 пп годовых
    fund_exit_pp: float = 25.0        # выход: < 25 пп (давление спало)
    fund_trail_days: int = 3          # трейл фандинга
    roll_days_before: int = 3         # ролл квартальной ноги за N дней до экспирации
    units: int = 1                    # юнитов на пару (юнит = perp_lots+quart_lots из реестра)
    fee_per_lot: float = 2.0
    # дивидендный фильтр (как st6): аномальный |базис| квартальника → входу нельзя верить
    basis_sane_pp: float = 25.0


class St7Config(BaseModel):
    strategy: St7StrategyConfig = St7StrategyConfig()
    mode: str = "paper"               # paper | tbank_sandbox
    account_id: str = ""
    poll_seconds: float = 600.0
    trading_enabled: bool = True
