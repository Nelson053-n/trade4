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


# ============================ St8Executor (paper + защиты) ============================

def _exec_paper(monkeypatch, audit=None):
    from app.st8.executor import St8Executor
    from app.st4 import tbank_sandbox as sb
    monkeypatch.setattr(sb, "find_share", lambda tk: {"uid": "sh_"+tk, "lot": 10 if tk=="NLMK" else 1})
    monkeypatch.setattr(sb, "find_future", lambda tk: {"uid": "fut_"+tk})
    return St8Executor("acc", paper=True, audit_cb=audit)


def test_executor_paper_open_close(monkeypatch):
    """Paper: вход акция+хедж и выход — виртуальные филлы, аудит фиксирует ордера."""
    orders = []
    e = _exec_paper(monkeypatch, audit=orders.append)
    r = e.open("SBER", stock_lots=5, stock_px=300.0, hedge_lots=1, hedge_px=2800.0)
    assert r["ok"] and r["stock_filled"] == 5 and r["hedge_filled"] == 1
    # 2 ордера входа: акция BUY + хедж SELL
    entries = [o for o in orders if o["op"].startswith("entry")]
    assert len(entries) == 2
    assert any(o["direction"] == "BUY" and o["op"] == "entry" for o in entries)
    assert any(o["direction"] == "SELL" and o["op"] == "entry_hedge" for o in entries)
    orders.clear()
    e.close("SBER", stock_lots=5, stock_px=310.0, hedge_lots=1, hedge_px=2820.0)
    # выход мелкими: 5 SELL акции + 1 BUY хедж
    assert sum(1 for o in orders if o["op"] == "exit") == 5
    assert sum(1 for o in orders if o["op"] == "exit_hedge") == 1


def test_executor_share_lot_size(monkeypatch):
    """Лотность акции берётся из справочника в sandbox (NLMK=10, SBER=1)."""
    from app.st8.executor import St8Executor
    from app.st4 import tbank_sandbox as sb
    monkeypatch.setattr(sb, "find_share", lambda tk: {"uid": "sh_"+tk, "lot": 10 if tk=="NLMK" else 1})
    e = St8Executor("acc", paper=False)   # sandbox: реальный резолв лотности
    assert e.share_lot("NLMK") == 10
    assert e.share_lot("SBER") == 1


def test_executor_hedge_fail_rolls_back_stock(monkeypatch):
    """Если хедж не исполнился — акция откатывается (не оставлять голую бету)."""
    from app.st8.executor import St8Executor, St8ExecError
    from app.st4 import tbank_sandbox as sb
    monkeypatch.setattr(sb, "find_share", lambda tk: {"uid": "sh_"+tk, "lot": 1})
    monkeypatch.setattr(sb, "find_future", lambda tk: {"uid": "fut_"+tk})
    orders = []
    e = St8Executor("acc", paper=False, audit_cb=orders.append)
    # акция налилась, хедж бросает ошибку
    def _post(acc, uid, lots, direction, oid):
        if uid.startswith("fut_"):
            raise RuntimeError("Not enough balance")
        return {"lotsExecuted": lots}
    monkeypatch.setattr(sb, "post_order", _post)
    try:
        e.open("SBER", stock_lots=3, stock_px=300.0, hedge_lots=1, hedge_px=2800.0)
        assert False, "должно бросить St8ExecError"
    except St8ExecError:
        pass
    # акция откачена мелкими SELL (unwind)
    unwinds = [o for o in orders if o["op"] == "unwind"]
    assert len(unwinds) == 3  # 3 лота по 1


def test_executor_partial_stock_fill_hedges_actual(monkeypatch):
    """Частичный филл акции (3 из 5) → хеджируем реально налитое, не откатываем."""
    from app.st8.executor import St8Executor
    from app.st4 import tbank_sandbox as sb
    monkeypatch.setattr(sb, "find_share", lambda tk: {"uid": "sh_"+tk, "lot": 1})
    monkeypatch.setattr(sb, "find_future", lambda tk: {"uid": "fut_"+tk})
    def _post(acc, uid, lots, direction, oid):
        return {"lotsExecuted": 3 if uid.startswith("sh_") else lots}
    monkeypatch.setattr(sb, "post_order", _post)
    e = St8Executor("acc", paper=False)
    r = e.open("SBER", stock_lots=5, stock_px=300.0, hedge_lots=1, hedge_px=2800.0)
    assert r["stock_filled"] == 3  # работаем с реально налитым


# ============================ daily-tick цикл (paper, end-to-end) ============================

def test_tick_enters_on_entry_day(monkeypatch):
    """Daily-tick в день входа (ex−10): вход в позицию с хеджем (paper)."""
    from app.st8.service import St8Session
    s = St8Session()
    s.cfg.mode = "paper"
    s.cfg.strategy.hedge_imoexf = True
    s.cfg.strategy.use_futures = False   # тест акционного пути (без HTTP-резолва фьючей)
    # только одна бумага для чистоты
    s.enabled = {tk: (tk == "TATN") for tk in s.enabled}
    # мокаем ISS: торговые дни, дивиденды, живые цены
    days = [f"2026-05-{d:02d}" for d in range(1, 31)]
    monkeypatch.setattr(s, "_load_trading_days", lambda since: setattr(s, "_trading_days", days))
    s._trading_days = days
    monkeypatch.setattr(s, "scan_new_dividends", lambda: [])
    monkeypatch.setattr(s, "_fetch_divs", lambda tk: [("2026-05-20", 35.0, 5.0)] if tk == "TATN" else [])
    # рынок открыт, цены есть
    def _refresh():
        s.market = {"TATN": {"last": 700.0, "bid": 699.5, "offer": 700.5, "spread_pct": 0.14}}
        s.hedge_px = 2800.0
    monkeypatch.setattr(s, "refresh_market", _refresh)
    monkeypatch.setattr(s, "refresh_capital", lambda: None)
    monkeypatch.setattr(s, "in_exec_window", lambda: True)   # план-сделки — в окне у закрытия
    monkeypatch.setattr(s, "save_session", lambda: None)
    # день входа = 2026-05-20 минус 10 торговых = days[index-10]
    ex_i = days.index("2026-05-20"); entry_day = days[ex_i - 10]
    import app.st8.service as svc, datetime as _dt
    class _FakeDate(_dt.date):
        @classmethod
        def today(cls): return _dt.date.fromisoformat(entry_day)
    monkeypatch.setattr(svc, "date", _FakeDate)
    r = s.tick()
    assert "TATN" in r["entered"]
    assert s.engines["TATN"].position is not None
    assert s.engines["TATN"].position.stock_entry == 700.5   # вход по ask (реализм)


def test_tick_missed_when_trading_off(monkeypatch):
    """Торговля выключена в день входа → упущенный вход залогирован."""
    from app.st8.service import St8Session
    s = St8Session()
    s.cfg.mode = "paper"; s.cfg.trading_enabled = False
    s.cfg.strategy.use_futures = False
    s.enabled = {tk: (tk == "TATN") for tk in s.enabled}
    days = [f"2026-05-{d:02d}" for d in range(1, 31)]
    s._trading_days = days
    monkeypatch.setattr(s, "_load_trading_days", lambda since: None)
    monkeypatch.setattr(s, "scan_new_dividends", lambda: [])
    monkeypatch.setattr(s, "_fetch_divs", lambda tk: [("2026-05-20", 35.0, 5.0)] if tk == "TATN" else [])
    monkeypatch.setattr(s, "refresh_market", lambda: setattr(s, "market", {"TATN": {"last": 700.0, "offer": 700.5}}) or setattr(s, "hedge_px", 2800.0))
    monkeypatch.setattr(s, "refresh_capital", lambda: None)
    monkeypatch.setattr(s, "in_exec_window", lambda: True)   # план-сделки — в окне у закрытия
    monkeypatch.setattr(s, "save_session", lambda: None)
    ex_i = days.index("2026-05-20"); entry_day = days[ex_i - 10]
    import app.st8.service as svc, datetime as _dt
    class _FakeDate2(_dt.date):
        @classmethod
        def today(cls): return _dt.date.fromisoformat(entry_day)
    monkeypatch.setattr(svc, "date", _FakeDate2)
    r = s.tick()
    assert r["missed"] == 1
    assert any(m["ticker"] == "TATN" and "выкл" in m["reason"] for m in s.missed)


def test_st8_in_ledger_recon():
    """ST8 включён в посделочную сверку (_daily_ledger_recon) — exit_date→exit_ts конверсия."""
    import datetime as _dtm
    from app.api import _daily_ledger_recon, ST8
    MSK = _dtm.timezone(_dtm.timedelta(hours=3))
    # paper st8 → должен пометиться "сверка не требуется"
    ST8.cfg.mode = "paper"; ST8.cfg.account_id = ""
    rows = _daily_ledger_recon("2026-05-20", MSK)
    st8_row = [r for r in rows if "ST8" in r[0]]
    assert st8_row and "paper" in st8_row[0][0]


def test_tick_skips_wide_spread(monkeypatch):
    """Широкий спред (> max_spread_pct) → вход пропущен, залогирован как дорогое исполнение."""
    import app.st8.service as svc
    from app.st8.service import St8Session
    import datetime as _dt
    s = St8Session()
    s.cfg.mode = "paper"; s.cfg.strategy.max_spread_pct = 0.25
    s.cfg.strategy.use_futures = False
    s.enabled = {tk: (tk == "MRKC") for tk in s.enabled}
    days = [f"2026-05-{d:02d}" for d in range(1, 31)]
    s._trading_days = days
    monkeypatch.setattr(s, "_load_trading_days", lambda since: None)
    monkeypatch.setattr(s, "scan_new_dividends", lambda: [])
    monkeypatch.setattr(s, "_fetch_divs", lambda tk: [("2026-05-20", 0.05, 10.0)] if tk == "MRKC" else [])
    # MRKC широкий спред 0.56%
    monkeypatch.setattr(s, "refresh_market", lambda: setattr(s, "market",
        {"MRKC": {"last": 0.5, "offer": 0.5014, "bid": 0.4986, "spread_pct": 0.56}}) or setattr(s, "hedge_px", 2800.0))
    monkeypatch.setattr(s, "refresh_capital", lambda: None)
    monkeypatch.setattr(s, "in_exec_window", lambda: True)   # план-сделки — в окне у закрытия
    monkeypatch.setattr(s, "save_session", lambda: None)
    ex_i = days.index("2026-05-20"); entry_day = days[ex_i - 10]
    class _FD(_dt.date):
        @classmethod
        def today(cls): return _dt.date.fromisoformat(entry_day)
    monkeypatch.setattr(svc, "date", _FD)
    r = s.tick()
    assert r["missed"] == 1
    assert any("спред" in m["reason"] for m in s.missed)


# ============================ шорт-нога «пост-дивидендное сдувание» ============================

def test_short_entry_on_ex_day():
    """Шорт входит ровно в день гэпа (ex_date), июль ТОРГУЕТСЯ (лучший месяц шорта)."""
    days_jul = [f"2026-07-{d:02d}" for d in range(1, 29)]
    e = _eng(short_enabled=True)
    ev = DivEvent("TATN", ex_date="2026-07-15", div=35.0, div_yield_pct=5.0)
    assert e.short_entry_signal("2026-07-15", [ev], days_jul) is ev   # день гэпа → сигнал
    assert e.short_entry_signal("2026-07-14", [ev], days_jul) is None # накануне — нет
    assert e.short_entry_signal("2026-07-16", [ev], days_jul) is None # после — нет


def test_short_skips_december():
    """Декабрь (−0.56%, новогоднее ралли) — шорт не входит."""
    days_dec = [f"2026-12-{d:02d}" for d in range(1, 29)]
    e = _eng(short_enabled=True, short_skip_months=[8, 12])
    ev = DivEvent("TATN", ex_date="2026-12-15", div=35.0, div_yield_pct=5.0)
    assert e.short_entry_signal("2026-12-15", [ev], days_dec) is None


def test_short_pnl_profits_on_fall():
    """Шорт: прибыль при падении акции (сдувание после отсечки)."""
    e = _eng(short_enabled=True, fee_rate=0.0)
    ev = DivEvent("TATN", ex_date="2026-05-15", div=35.0, div_yield_pct=5.0)
    e.open("2026-05-15", ev, stock_px=700.0, hedge_px=0.0, hedge_lots=0, side="short")
    assert e.position.side == "short"
    # акция упала 700 → 680: шорт зарабатывает +20/акцию
    tr = e.close("2026-05-22", stock_px=680.0, hedge_px=0.0, reason="exit")
    assert abs(tr.stock_pnl_rub - 20.0) < 0.01
    assert tr.side == "short"
    # и наоборот: рост акции = убыток шорта
    e.open("2026-05-15", ev, stock_px=700.0, hedge_px=0.0, hedge_lots=0, side="short")
    tr2 = e.close("2026-05-22", stock_px=720.0, hedge_px=0.0, reason="stop")
    assert abs(tr2.stock_pnl_rub - (-20.0)) < 0.01


def test_short_exit_after_hold_days():
    """Выкуп шорта через short_hold_days торговых дней после ex."""
    e = _eng(short_enabled=True, short_hold_days=5)
    ev = DivEvent("TATN", ex_date="2026-05-15", div=35.0, div_yield_pct=5.0)
    e.open("2026-05-15", ev, stock_px=700.0, hedge_px=0.0, hedge_lots=0, side="short")
    ex_i = DAYS.index("2026-05-15")
    assert e.short_exit_day(DAYS) == DAYS[ex_i + 5]
    # а лонг-выход для шорта не срабатывает
    assert e.exit_day(DAYS) is None


def test_short_stop_on_rise():
    """Стоп шорта: акция ВЫРОСЛА сильнее stop_loss_pct → стоп бьёт."""
    e = _eng(short_enabled=True, stop_loss_pct=5.0, fee_rate=0.0)
    ev = DivEvent("TATN", ex_date="2026-05-15", div=35.0, div_yield_pct=5.0)
    e.open("2026-05-15", ev, stock_px=700.0, hedge_px=0.0, hedge_lots=0, side="short")
    assert e.check_stop(720.0, 0.0) is False   # +2.9% — в пределах
    assert e.check_stop(740.0, 0.0) is True    # +5.7% против шорта — стоп


# ============================ сайзинг (сумма входа) + фьючерсы ============================

def test_position_lots_manual_rub():
    """manual_rub: лоты = сумма / (цена × пункт-стоимость)."""
    from app.st8.service import St8Session
    s = St8Session()
    s.cfg.strategy.sizing_mode = "manual_rub"
    s.cfg.strategy.entry_notional_rub = 100_000.0
    assert s._position_lots(700.0, 1.0) == 142      # акция 700₽ → 142 лота
    assert s._position_lots(45_000.0, 1.0) == 2     # фьючерс 45к пунктов pv=1 → 2 лота
    assert s._position_lots(700.0, 10.0) == 14      # лотность 10 акций


def test_position_lots_cash_pct(monkeypatch):
    """cash_pct: лоты от % свободного кэша."""
    from app.st8.service import St8Session
    s = St8Session()
    s.cfg.strategy.sizing_mode = "cash_pct"
    s.cfg.strategy.entry_cash_pct = 25.0
    monkeypatch.setattr(s, "free_cash_rub", lambda: 1_000_000.0)
    assert s._position_lots(500.0, 1.0) == 500      # 250к / 500₽


def test_free_cash_paper_subtracts_positions():
    """paper: свободный кэш = капитал − нотионалы открытых позиций."""
    from app.st8.service import St8Session
    from app.st8.engine import DivEvent
    s = St8Session()
    s.cfg.mode = "paper"; s.capital_rub = 1_000_000.0
    eng = s._engine("TATN")
    eng.strat.quantity_lots = 100
    ev = DivEvent("TATN", "2026-05-20", 35.0, 5.0)
    eng.open("2026-05-06", ev, stock_px=700.0, hedge_px=0.0, hedge_lots=0)
    # занято 700×100×1 = 70000
    assert abs(s.free_cash_rub() - 930_000.0) < 1


def test_tick_enters_via_futures(monkeypatch):
    """use_futures: вход исполняется фьючерсом (инструмент в позиции, цена фьюча, pv)."""
    import app.st8.service as svc
    from app.st8.service import St8Session
    import datetime as _dt
    s = St8Session()
    s.cfg.mode = "paper"
    s.cfg.strategy.use_futures = True
    s.cfg.strategy.hedge_imoexf = False
    s.cfg.strategy.sizing_mode = "manual_rub"; s.cfg.strategy.entry_notional_rub = 100_000
    s.enabled = {tk: (tk == "TATN") for tk in s.enabled}
    days = [f"2026-05-{d:02d}" for d in range(1, 31)]
    s._trading_days = days
    monkeypatch.setattr(s, "_load_trading_days", lambda since: None)
    monkeypatch.setattr(s, "scan_new_dividends", lambda: [])
    monkeypatch.setattr(s, "sleeping_tickers", lambda: [])
    monkeypatch.setattr(s, "_fetch_divs", lambda tk: [("2026-05-20", 35.0, 5.0)] if tk == "TATN" else [])
    monkeypatch.setattr(s, "refresh_market", lambda: (s.market.update({"TATN": {"last": 700.0, "bid": 699.5, "offer": 700.5}}), setattr(s, "hedge_px", 2800.0))[0])
    # мок фьючерсного резолва: TTU6 с котировками и pv=1 (пункт=1₽, цена в пунктах ≈ 10 акций)
    monkeypatch.setattr(s, "_instrument_for", lambda tk, ex, hold_after=7: ("TTU6", {"last": 7000.0, "bid": 6995.0, "offer": 7005.0}, 1.0, 1))
    monkeypatch.setattr(s, "refresh_capital", lambda: None)
    monkeypatch.setattr(s, "in_exec_window", lambda: True)   # план-сделки — в окне у закрытия
    monkeypatch.setattr(s, "save_session", lambda: None)
    ex_i = days.index("2026-05-20"); entry_day = days[ex_i - 10]
    class _FD(_dt.date):
        @classmethod
        def today(cls): return _dt.date.fromisoformat(entry_day)
    monkeypatch.setattr(svc, "date", _FD)
    r = s.tick()
    assert "TATN" in r["entered"]
    p = s.engines["TATN"].position
    assert p.instrument == "TTU6"          # исполнено фьючерсом
    assert p.stock_entry == 7005.0         # по ask фьюча
    assert p.lots == 14                    # 100000 / 7005 / 1


# ==================== регрессии ревизии 11.07 ====================

def test_trading_days_projected_into_future(monkeypatch):
    """Календарь = история + проекция будущих рабочих дней. Без проекции лонг не входил
    НИКОГДА (будущая ex-дата отсутствовала в календаре), а _prev_trading_day для будущей
    регдаты возвращал вчерашний день → ложный ex и преждевременный шорт."""
    import app.st8.service as svc
    from app.st8.service import St8Session
    import datetime as _dt
    s = St8Session()
    hist = ["2026-07-08", "2026-07-09", "2026-07-10"]      # история кончается «вчера»
    monkeypatch.setattr(svc, "_iss", lambda url: {"history": {"data": [[d] for d in hist]}})
    class _FD(_dt.date):
        @classmethod
        def today(cls): return _dt.date(2026, 7, 11)       # суббота
    monkeypatch.setattr(svc, "date", _FD)
    s._load_trading_days("2026-07-01")
    days = s._trading_days
    assert days[:3] == hist
    assert "2026-07-13" in days and "2026-07-14" in days   # будущие будни есть
    assert "2026-07-12" not in days                        # воскресенье — нет
    # регдата в будущем (пт 24.07) → ex = четверг 23.07, а НЕ последний исторический день
    assert s._prev_trading_day("2026-07-24") == "2026-07-23"


def test_entry_signal_fires_for_future_ex(monkeypatch):
    """Интеграция: с проекцией будущего лонг-вход срабатывает за 10 торг.дней до ex."""
    from app.st8.engine import St8Engine, DivEvent
    from app.st8.config import St8StrategyConfig
    # календарь: история (до 10-го) + проекция (11-е и дальше) — как строит сервис
    days = [f"2026-06-{d:02d}" for d in range(1, 31)]
    e = St8Engine("TATN", St8StrategyConfig(), lot_size=1)
    ev = DivEvent("TATN", "2026-06-25", 35.0, 5.0)         # ex в «будущем» списка
    entry_day = days[days.index("2026-06-25") - 10]
    assert e.entry_signal(entry_day, [ev], days) is ev


def test_open_fee_uses_actual_lots():
    """Комиссия входа — от фактических лотов (была от quantity_lots=1 при перезаписи
    position.lots постфактум → занижение комиссий в разы)."""
    from app.st8.engine import St8Engine, DivEvent
    from app.st8.config import St8StrategyConfig
    e = St8Engine("TATN", St8StrategyConfig(fee_rate=0.001), lot_size=1)
    ev = DivEvent("TATN", "2026-05-20", 35.0, 5.0)
    e.open("2026-05-06", ev, stock_px=700.0, hedge_px=0.0, hedge_lots=0, lots=5)
    assert e.position.lots == 5
    assert abs(e.position.fees_rub - 700.0 * 5 * 0.001) < 0.01   # 3.5₽, не 0.7₽


def test_unit_value_does_not_mutate_lot_size():
    """unit_value живёт в позиции: после фьючерсной сделки следующий вход АКЦИЕЙ
    использует родную лотность (мутация engine.lot_size ломала сайзинг/P&L)."""
    from app.st8.engine import St8Engine, DivEvent
    from app.st8.config import St8StrategyConfig
    e = St8Engine("NLMK", St8StrategyConfig(fee_rate=0.0), lot_size=10)
    ev = DivEvent("NLMK", "2026-05-20", 25.0, 5.0)
    e.open("2026-05-06", ev, stock_px=20_000.0, hedge_px=0.0, hedge_lots=0,
           instrument="NLU6", unit_value=1.0, lots=5)      # фьючерс: pv=1
    tr = e.close("2026-05-18", 20_100.0, 0.0, "exit")
    assert abs(tr.stock_pnl_rub - 100.0 * 5 * 1.0) < 0.01  # P&L через pv, не лотность
    assert e.lot_size == 10                                # лотность акции НЕ тронута
    e.open("2026-05-25", ev, stock_px=200.0, hedge_px=0.0, hedge_lots=0, lots=2)
    tr2 = e.close("2026-05-27", 201.0, 0.0, "exit")
    assert abs(tr2.stock_pnl_rub - 1.0 * 2 * 10) < 0.01    # акция: снова через лотность


def test_positions_persist_roundtrip(tmp_path):
    """Позиции st8 переживают рестарт (раньше не персистились вовсе: рестарт с открытой
    позицией терял её, лоты оставались на счёте бесхозными)."""
    from app.st8.service import St8Session
    from app.st8.engine import DivEvent
    s = St8Session()
    s._session_file = tmp_path / "s8.json"
    ev = DivEvent("TATN", "2026-05-20", 35.0, 5.0)
    s._engine("TATN").open("2026-05-06", ev, stock_px=700.0, hedge_px=2800.0,
                           hedge_lots=2, instrument="TTU6", unit_value=1.0, lots=3)
    s.save_session()
    s2 = St8Session()
    s2._session_file = s._session_file
    assert s2.load_session()
    p = s2.engines["TATN"].position
    assert p is not None and p.lots == 3 and p.side == "long"
    assert p.instrument == "TTU6" and p.unit_value == 1.0 and p.hedge_lots == 2


def test_exit_skipped_without_quote(monkeypatch):
    """Нет котировки исполнителя → выход откладывается (прежний fallback на цену входа
    писал в журнал фиктивный «выход в ноль»)."""
    import app.st8.service as svc
    from app.st8.service import St8Session
    from app.st8.engine import DivEvent
    import datetime as _dt
    s = St8Session()
    s.cfg.mode = "paper"
    s.enabled = {tk: (tk == "TATN") for tk in s.enabled}
    days = [f"2026-05-{d:02d}" for d in range(1, 31)]
    s._trading_days = days; s._tdays_date = "2026-05-12"
    monkeypatch.setattr(s, "_load_trading_days", lambda since: None)
    monkeypatch.setattr(s, "scan_new_dividends", lambda: [])
    monkeypatch.setattr(s, "sleeping_tickers", lambda: [])
    monkeypatch.setattr(s, "_fetch_divs", lambda tk: [])
    # рынок «открыт» другой бумагой, но у TATN котировок нет
    monkeypatch.setattr(s, "refresh_market", lambda: s.market.update(
        {"MOEX": {"last": 200.0}}))
    monkeypatch.setattr(s, "refresh_capital", lambda: None)
    monkeypatch.setattr(s, "save_session", lambda: None)
    monkeypatch.setattr(s, "in_exec_window", lambda: True)
    ev = DivEvent("TATN", "2026-05-20", 35.0, 5.0)
    s._engine("TATN").open("2026-05-06", ev, stock_px=700.0, hedge_px=0.0, hedge_lots=0)
    class _FD(_dt.date):
        @classmethod
        def today(cls): return _dt.date(2026, 5, 20)       # день давно за out_day
    monkeypatch.setattr(svc, "date", _FD)
    s.tick()
    assert s.engines["TATN"].position is not None          # позиция ждёт котировку
    assert not s.trades


def test_daily_loss_limit_halts_entries(monkeypatch):
    """Дневной лимит убытка: реализованный минус за день → входы HALT (missed), выходы живы."""
    import app.st8.service as svc
    from app.st8.service import St8Session
    import datetime as _dt
    s = St8Session()
    s.cfg.mode = "paper"
    s.cfg.strategy.daily_loss_limit_rub = 1000.0
    s.cfg.strategy.use_futures = False
    s.enabled = {tk: (tk == "TATN") for tk in s.enabled}
    days = [f"2026-05-{d:02d}" for d in range(1, 31)]
    ex_i = days.index("2026-05-20"); entry_day = days[ex_i - 10]
    s._trading_days = days; s._tdays_date = entry_day
    s.trades = [{"ticker": "MOEX", "exit_date": entry_day, "net_pnl_rub": -1500.0}]
    monkeypatch.setattr(s, "_load_trading_days", lambda since: None)
    monkeypatch.setattr(s, "scan_new_dividends", lambda: [])
    monkeypatch.setattr(s, "sleeping_tickers", lambda: [])
    monkeypatch.setattr(s, "_fetch_divs", lambda tk: [("2026-05-20", 35.0, 5.0)] if tk == "TATN" else [])
    monkeypatch.setattr(s, "refresh_market", lambda: s.market.update(
        {"TATN": {"last": 700.0, "bid": 699.5, "offer": 700.5, "spread_pct": 0.14}}))
    monkeypatch.setattr(s, "refresh_capital", lambda: None)
    monkeypatch.setattr(s, "save_session", lambda: None)
    monkeypatch.setattr(s, "in_exec_window", lambda: True)
    class _FD(_dt.date):
        @classmethod
        def today(cls): return _dt.date.fromisoformat(entry_day)
    monkeypatch.setattr(svc, "date", _FD)
    r = s.tick()
    assert r["missed"] == 1 and not r["entered"]
    assert any("лимит" in m["reason"] for m in s.missed)


# ==================== боевой контур tbank_real (двойной гейт, канон st5) ====================

def test_real_order_blocked_when_not_armed(monkeypatch):
    """real=True без взвода: ЛЮБОЙ ордер заблокирован, боевой API не вызывается."""
    import app.st8.executor as exmod
    from app.st8.executor import St8Executor, St8ExecError
    import pytest
    called = []
    monkeypatch.setattr(exmod._live, "post_order",
                        lambda *a, **k: called.append(a) or {})
    e = St8Executor("real-acc", paper=False, real=True, armed_cb=lambda: False)
    with pytest.raises(St8ExecError, match="не взведена"):
        e._order("uid1", 1, "BUY", "entry", 100.0)
    assert not called                                  # боевой ордер НЕ ушёл


def test_real_order_armed_routes_to_live(monkeypatch):
    """Взведённый real: ордер идёт в БОЕВОЙ OrdersService с идемпотентным sha256-id."""
    import app.st8.executor as exmod
    from app.st8.executor import St8Executor
    calls = []
    def _post(acc, uid, lots, direction, oid, **kw):
        calls.append({"acc": acc, "uid": uid, "lots": lots, "dir": direction, "oid": oid})
        return {"executionReportStatus": "FILL", "lotsExecuted": lots}
    monkeypatch.setattr(exmod._live, "post_order", _post)
    monkeypatch.setattr(exmod._sb, "post_order",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("sandbox path!")))
    monkeypatch.setattr(exmod._sb, "last_price", lambda uid: 100.0)
    e = St8Executor("real-acc", paper=False, real=True, armed_cb=lambda: True)
    r = e._order("uid1", 2, "BUY", "entry", 100.0)
    assert r["lotsExecuted"] == 2
    assert len(calls) == 1 and calls[0]["acc"] == "real-acc"
    oid = calls[0]["oid"]
    assert len(oid) == 32 and "-" not in oid           # sha256-хеш, не UUID


def test_real_order_price_sanity(monkeypatch):
    """Pre-trade sanity: рынок уехал >5% от ref → боевой ордер отклонён."""
    import app.st8.executor as exmod
    from app.st8.executor import St8Executor, St8ExecError
    import pytest
    monkeypatch.setattr(exmod._sb, "last_price", lambda uid: 120.0)   # +20% от ref
    monkeypatch.setattr(exmod._live, "post_order",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("не должен уйти")))
    e = St8Executor("real-acc", paper=False, real=True, armed_cb=lambda: True)
    with pytest.raises(St8ExecError, match="аномальная цена"):
        e._order("uid1", 1, "BUY", "entry", 100.0)


def test_real_armed_cooldown():
    """Взвод действует только после cooldown 600с от старта live."""
    import time
    from app.st8.service import St8Session
    s = St8Session()
    s.state["real_trading_armed"] = True
    s.state["session_started"] = time.time()           # только что стартовали
    assert s._real_armed() is False
    s.state["session_started"] = time.time() - 700     # cooldown прошёл
    assert s._real_armed() is True
    s.state["real_trading_armed"] = False
    assert s._real_armed() is False


def test_position_lots_real_notional_cap():
    """В tbank_real сайзинг режется потолком real_max_notional_rub (пилот малым размером)."""
    from app.st8.service import St8Session
    s = St8Session()
    s.cfg.strategy.sizing_mode = "manual_rub"
    s.cfg.strategy.entry_notional_rub = 500_000.0
    s.cfg.strategy.real_max_notional_rub = 100_000.0
    s.cfg.mode = "tbank_real"
    assert s._position_lots(700.0, 1.0) == 142         # 100к/700, не 500к/700
    s.cfg.mode = "tbank_sandbox"
    assert s._position_lots(700.0, 1.0) == 714         # песочница без потолка


def test_per_ticker_override_applies_to_engine():
    """Per-ticker оверрайд entry_days_before/stop применяется к движку тикера, не к другим."""
    from app.st8.service import St8Session
    s = St8Session()
    s.update_ticker("ROSN", {"entry_days_before": 7, "stop_loss_pct": 3.0})
    assert s._engine("ROSN").strat.entry_days_before == 7
    assert s._engine("ROSN").strat.stop_loss_pct == 3.0
    # другой тикер — глобальные значения
    assert s._engine("MGNT").strat.entry_days_before == s.cfg.strategy.entry_days_before


def test_per_ticker_notional_via_p():
    """Нотионал: per-ticker через _p, остальные тикеры — глобал."""
    from app.st8.service import St8Session
    s = St8Session()
    s.update_ticker("ROSN", {"entry_notional_rub": 50_000})
    assert s._p("ROSN", "entry_notional_rub") == 50_000
    assert s._p("MGNT", "entry_notional_rub") == s.cfg.strategy.entry_notional_rub


def test_per_ticker_clear_override():
    """None снимает конкретный оверрайд (возврат к глобалу), не трогая остальные."""
    from app.st8.service import St8Session
    s = St8Session()
    s.update_ticker("ROSN", {"entry_days_before": 7, "stop_loss_pct": 3.0})
    s.update_ticker("ROSN", {"entry_days_before": None})
    eff = s.update_ticker("ROSN", {})["effective"]
    assert eff["entry_days_before"] == s.cfg.strategy.entry_days_before   # снят
    assert eff["stop_loss_pct"] == 3.0                                    # остался


def test_per_ticker_validation():
    """Гейты: неизвестный тикер и значение вне диапазона."""
    import pytest
    from app.st8.service import St8Session
    s = St8Session()
    with pytest.raises(ValueError, match="неизвестный тикер"):
        s.update_ticker("XXXX", {"stop_loss_pct": 2})
    with pytest.raises(ValueError, match="вне"):
        s.update_ticker("MGNT", {"stop_loss_pct": 99})


def test_per_ticker_persists():
    """Оверрайды переживают рестарт (save→load)."""
    import tempfile, os
    from pathlib import Path
    from app.st8.service import St8Session
    s = St8Session()
    tf = tempfile.mktemp(suffix=".json")
    s._session_file = Path(tf)
    s.update_ticker("ROSN", {"entry_days_before": 7, "stop_loss_pct": 3.0})
    s.save_session()
    s2 = St8Session()
    s2._session_file = Path(tf)
    s2.load_session()
    assert s2._engine("ROSN").strat.entry_days_before == 7
    assert s2.ticker_overrides.get("ROSN") == {"entry_days_before": 7, "stop_loss_pct": 3.0}
    os.unlink(tf)


def test_flat_all_closes_and_journals():
    """flat_all закрывает открытую позицию и пишет её в журнал (paper)."""
    from app.st8.service import St8Session
    from app.st8.engine import St8Position
    s = St8Session()
    tk = list(__import__("app.st8.service", fromlist=["ST8_TICKERS"]).ST8_TICKERS)[0]
    eng = s._engine(tk)
    eng.position = St8Position(ticker=tk, entry_date="2026-10-01", ex_date="2026-10-15",
                               lots=5, stock_entry=300.0, hedge_lots=0, hedge_entry=0.0,
                               side="long", instrument="", unit_value=10.0)
    s.market[tk] = {"bid": 310.0, "offer": 311.0, "last": 310.5}
    before = len(s.trades)
    r = s.flat_all()
    assert len(r["closed"]) == 1
    assert eng.position is None
    assert len(s.trades) == before + 1
    assert s.trades[-1]["reason"] == "flat_all"
    # идемпотентность
    assert s.flat_all()["closed"] == []
