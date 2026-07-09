"""Конфигурация ST9 — «трендовая корзина» (Donchian-пробой + ATR-трейлинг, 60м бары).

ЧЕСТНАЯ РАМКА (итог поиска 08.07, длинная история 7.5 лет): НИ ОДИН трендер соло не
проходит строгий гейт (все годы+) — золото оказалось артефактом окна 2023-26. Ценность
только в КОРЗИНЕ независимых слабых трендеров (механика AHL/Winton): ~30-50%/год на
капитал при плече 2-3×, DD 20-30%, НЕ каждый год плюс. Это НАПРАВЛЕННАЯ ставка с плечом,
не market-neutral. В плане 100%+ st9 — диверсификатор к st8-сэндвичу, не ядро.

MVP v1 — два НЕЗАВИСИМЫХ перпетуала (без роллов), 60м бары:
- Si (USDRUBF, FX-девальвация): Donchian 20/10 — в окне перпа +26.9% нотионала/год,
  PF 1.51, 122 сд/год, ПЕРЕЖИЛ 2022 на 60м (+1%), 3-way ✓
- GOLD (GLDRUBF, мировое золото): Donchian 32/16 — +39.6%/год, PF 1.75 (окно 2023-26!)
corr(Si,Gold) ≈ 0 — разные драйверы. v2: + GAZR (дневки 7 лет +25%/год) и BR (нефть).
Граница частоты: удержание ≥1.5-2 дня = edge жив; быстрые окна = смерть band-стилем.
"""
from __future__ import annotations

from pydantic import BaseModel


class St9InstrumentCfg(BaseModel):
    secid: str                        # перпетуал FORTS (USDRUBF/GLDRUBF)
    don_enter: int = 20               # окно пробоя входа (60м баров)
    don_exit: int = 10                # окно противопробоя выхода
    atr_mult: float = 3.0             # ATR(14)-трейлинг множитель
    entry_notional_rub: float = 100_000.0   # нотионал позиции на инструмент


class St9StrategyConfig(BaseModel):
    atr_period: int = 14
    allow_short: bool = True          # тренд двусторонний (шорт Si = укрепление рубля)
    fee_per_lot: float = 2.0          # ₽/лот/сторона (перпы дёшевы)
    daily_loss_limit_rub: float = 0.0


class St9Config(BaseModel):
    strategy: St9StrategyConfig = St9StrategyConfig()
    instruments: list[St9InstrumentCfg] = [
        St9InstrumentCfg(secid="USDRUBF", don_enter=20, don_exit=10),
        St9InstrumentCfg(secid="GLDRUBF", don_enter=32, don_exit=16),
    ]
    mode: str = "paper"               # paper | tbank_sandbox
    account_id: str = ""
    poll_seconds: float = 600.0       # 60м бары — опрос раз в 10 мин достаточно
    trading_enabled: bool = True
