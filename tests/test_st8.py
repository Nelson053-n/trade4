"""Тесты ST8 — «дивидендный набег»: событийные сигналы, хедж-P&L, стоп, июль-фильтр."""
from app.st8.config import St8StrategyConfig
from app.st8.engine import St8Engine, DivEvent


def _eng(**kw):
    return St8Engine("TATN", St8StrategyConfig(**kw), lot_size=1, pv_hedge=10.0)


# 30 подряд торговых дней 2026-05
DAYS = [f"2026-05-{d:02d}" for d in range(1, 29)]


def test_entry_signal_n_days_before_ex():
    """Вход ровно за entry_days_before торговых дней до ex-даты."""
    e = _eng(entry_days_before=10)
    ev = DivEvent("TATN", ex_date="2026-05-20", div=35.0, div_yield_pct=5.0)
    ex_i = DAYS.index("2026-05-20")
    entry_day = DAYS[ex_i - 10]              # 2026-05-06
    # в день входа — сигнал есть
    assert e.entry_signal(entry_day, [ev], DAYS) is ev
    # в другой день — нет
    assert e.entry_signal(DAYS[ex_i - 9], [ev], DAYS) is None
    assert e.entry_signal(DAYS[ex_i - 11], [ev], DAYS) is None


def test_july_skipped():
    """Отсечки июля не торгуются (edge отрицателен, дивсезон переразогрет)."""
    days_jul = [f"2026-07-{d:02d}" for d in range(1, 29)]
    e = _eng(entry_days_before=5, skip_july=True)
    ev = DivEvent("TATN", ex_date="2026-07-15", div=35.0, div_yield_pct=5.0)
    ex_i = days_jul.index("2026-07-15")
    entry_day = days_jul[ex_i - 5]
    assert e.entry_signal(entry_day, [ev], days_jul) is None   # июль → None
    # с skip_july=False — сигнал есть
    e2 = _eng(entry_days_before=5, skip_july=False)
    assert e2.entry_signal(entry_day, [ev], days_jul) is ev


def test_min_div_yield_filter():
    """Мелкие дивиденды (< min_div_yield_pct) не торгуются."""
    e = _eng(entry_days_before=5, min_div_yield_pct=2.0)
    small = DivEvent("TATN", ex_date="2026-05-20", div=1.0, div_yield_pct=0.5)
    ex_i = DAYS.index("2026-05-20")
    assert e.entry_signal(DAYS[ex_i - 5], [small], DAYS) is None


def test_exit_day_before_gap():
    """Плановый выход = ex − exit_offset_days (накануне гэпа)."""
    e = _eng(entry_days_before=10, exit_offset_days=1)
    ev = DivEvent("TATN", ex_date="2026-05-20", div=35.0, div_yield_pct=5.0)
    e.open(DAYS[DAYS.index("2026-05-20") - 10], ev, stock_px=700.0, hedge_px=2800.0, hedge_lots=0)
    ex_i = DAYS.index("2026-05-20")
    assert e.exit_day(DAYS) == DAYS[ex_i - 1]   # накануне ex


def test_pnl_with_hedge_removes_beta():
    """Хедж шортом IMOEXF гасит рыночную бету: если и акция, и рынок выросли на ту же долю,
    чистый P&L ≈ только идиосинкразический рост акции (тут — набег сверх рынка)."""
    e = _eng(entry_days_before=10, hedge_imoexf=True, fee_rate=0.0)
    ev = DivEvent("TATN", ex_date="2026-05-20", div=35.0, div_yield_pct=5.0)
    # вход: акция 700 (1 лот=1 акция), хедж IMOEXF 2800, 1 лот шорт (нотионал ~28000 vs 700 —
    # для теста возьмём hedge_lots так, чтобы нотионалы сопоставимы условно)
    e.open("2026-05-06", ev, stock_px=700.0, hedge_px=2800.0, hedge_lots=1)
    # сценарий: акция +5% (735), рынок IMOEXF +2% (2856). Хедж-шорт теряет на росте рынка.
    tr = e.close("2026-05-19", stock_px=735.0, hedge_px=2856.0, reason="exit")
    # акция: +35₽; хедж-шорт IMOEXF: −(2856−2800)*1*10 = −560₽
    assert abs(tr.stock_pnl_rub - 35.0) < 0.01
    assert abs(tr.hedge_pnl_rub - (-560.0)) < 0.01
    assert abs(tr.net_pnl_rub - (35.0 - 560.0)) < 0.01


def test_pnl_no_hedge():
    """Без хеджа P&L = только нога акции."""
    e = _eng(hedge_imoexf=False, fee_rate=0.0)
    ev = DivEvent("TATN", ex_date="2026-05-20", div=35.0, div_yield_pct=5.0)
    e.open("2026-05-06", ev, stock_px=700.0, hedge_px=0.0, hedge_lots=0)
    tr = e.close("2026-05-19", stock_px=735.0, hedge_px=0.0, reason="exit")
    assert abs(tr.stock_pnl_rub - 35.0) < 0.01
    assert tr.hedge_pnl_rub == 0.0
    assert abs(tr.net_pnl_rub - 35.0) < 0.01


def test_stop_loss():
    """Стоп-лосс: чистый убыток > stop_loss_pct нотионала → check_stop True."""
    e = _eng(stop_loss_pct=5.0, hedge_imoexf=False, fee_rate=0.0)
    ev = DivEvent("TATN", ex_date="2026-05-20", div=35.0, div_yield_pct=5.0)
    e.open("2026-05-06", ev, stock_px=700.0, hedge_px=0.0, hedge_lots=0)
    # акция упала на 3% (679) — в пределах стопа 5%
    assert e.check_stop(679.0, 0.0) is False
    # акция упала на 6% (658) — стоп бьёт
    assert e.check_stop(658.0, 0.0) is True


def test_fees_round_trip():
    """Комиссия round-trip: вход (в open) + выход (в close), обе ноги."""
    e = _eng(hedge_imoexf=False, fee_rate=0.001)   # 0.1%
    ev = DivEvent("TATN", ex_date="2026-05-20", div=35.0, div_yield_pct=5.0)
    e.open("2026-05-06", ev, stock_px=700.0, hedge_px=0.0, hedge_lots=0)
    tr = e.close("2026-05-19", stock_px=700.0, hedge_px=0.0, reason="exit")
    # нотионал 700, комиссия 0.1% × 2 (вход+выход) = 1.4₽
    assert abs(tr.fees_rub - 1.4) < 0.01
    assert abs(tr.net_pnl_rub - (-1.4)) < 0.01   # цена не изменилась → минус комиссии
