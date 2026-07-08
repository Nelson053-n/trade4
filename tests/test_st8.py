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
    s.enabled = {tk: (tk == "TATN") for tk in s.enabled}
    days = [f"2026-05-{d:02d}" for d in range(1, 31)]
    s._trading_days = days
    monkeypatch.setattr(s, "_load_trading_days", lambda since: None)
    monkeypatch.setattr(s, "scan_new_dividends", lambda: [])
    monkeypatch.setattr(s, "_fetch_divs", lambda tk: [("2026-05-20", 35.0, 5.0)] if tk == "TATN" else [])
    monkeypatch.setattr(s, "refresh_market", lambda: setattr(s, "market", {"TATN": {"last": 700.0, "offer": 700.5}}) or setattr(s, "hedge_px", 2800.0))
    monkeypatch.setattr(s, "refresh_capital", lambda: None)
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
