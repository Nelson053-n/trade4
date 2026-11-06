"""Регрессии учёта реальных исполнений; сеть и торговые операции заменены ответами."""
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from app.st4.broker_accounting import all_operations, order_fill


def money(value):
    return {"units": str(int(value)), "nano": round((value - int(value)) * 1e9)}


def test_fill_price_is_per_instrument_not_divided_by_lots():
    f = order_fill({"lotsExecuted": "15", "executedOrderPrice": money(24860),
                    "executedCommission": money(186.45), "orderId": "order"}, 10)
    assert f == {"lots": 15, "price": 2486, "fee": 186.45, "order_id": "order"}
    assert order_fill({"lotsExecuted": 0}, 10)["lots"] == 0
    assert order_fill({}, 10)["fee"] is None
    assert order_fill({}, 10)["price"] is None


def test_operations_all_pages_deduplicated_and_normalized():
    calls = []
    def call(ns, method, params, **kw):
        calls.append((method, params, kw))
        if "cursor" not in params:
            return {"items": [{"id": "a", "type": "OPERATION_TYPE_BUY", "date": "1"}],
                    "hasNext": True, "nextCursor": "next"}
        return {"items": [{"id": "a", "type": "OPERATION_TYPE_BUY", "date": "1"},
                           {"id": "b", "type": "OPERATION_TYPE_BROKER_FEE", "date": "2"}],
                "hasNext": False}
    rows = all_operations(call, "sandbox", "cursor", "account", "from", "to", token="test")
    assert [r["id"] for r in rows] == ["a", "b"]
    assert rows[1]["operationType"] == "OPERATION_TYPE_BROKER_FEE"
    assert calls[1][1]["cursor"] == "next"
    assert calls[0][1]["state"] == "OPERATION_STATE_EXECUTED"
    assert calls[1][2] == {"token": "test"}


def test_operations_stuck_cursor_is_not_success():
    with pytest.raises(ValueError, match="не продвигается"):
        all_operations(lambda *a, **kw: {"items": [], "hasNext": True, "nextCursor": "same"},
                       "ns", "method", "acc", "from", "to")


def st8_session(tmp_path, monkeypatch):
    from app.st8.service import St8Session
    from app.st8.engine import DivEvent
    s = St8Session()
    s._session_file = tmp_path / "st8.json"
    eng = s._engine("TATN")
    eng.lot_size = 10
    eng.open("2026-10-01", DivEvent("TATN", "2026-10-15", 1, 1), 300, 0, 0,
             lots=5, stock_fee=5, hedge_fee=0, accounting_complete=True)
    s.market["TATN"] = {"bid": 310, "last": 310}
    return s, eng


def test_st8_service_partial_exit_keeps_all_pnl_and_fees(tmp_path, monkeypatch):
    s, eng = st8_session(tmp_path, monkeypatch)
    answers = iter([{"stock_closed": 2, "hedge_closed": 0,
                     "stock_fill": {"price": 310, "fee": 2}},
                    {"stock_closed": 3, "hedge_closed": 0,
                     "stock_fill": {"price": 310, "fee": 3}}])
    monkeypatch.setattr(s, "_exec", lambda: SimpleNamespace(close=lambda *a, **kw: next(answers)))
    result = s.flat_all()
    assert result["closed"] == []
    assert eng.position.lots == 3
    assert eng.position.fees_rub == pytest.approx(3)
    assert len(s.trades) == 1
    assert s.trades[0]["net_pnl_rub"] == 196
    s.flat_all()
    assert eng.position is None
    assert sum(t["net_pnl_rub"] for t in s.trades) == 490
    assert all(t["accounting_complete"] for t in s.trades)
    from app.st8.service import St8Session
    restored = St8Session()
    restored._session_file = s._session_file
    assert restored.load_session()
    assert len(restored.trades) == 2


def test_st8_asymmetric_hedge_close_preserves_entry_commissions():
    from app.st8.engine import St8Engine, DivEvent
    from app.st8.config import St8StrategyConfig
    eng = St8Engine("TATN", St8StrategyConfig(), lot_size=1)
    eng.open("2026-10-01", DivEvent("TATN", "2026-10-15", 1, 1), 100, 1000, 2,
             lots=5, stock_fee=5, hedge_fee=20, accounting_complete=True)
    first = eng.close_partial("2026-10-02", 110, 990, 5, 0, "partial", 5, True)
    assert first.net_pnl_rub == 40
    assert eng.position.lots == 0 and eng.position.hedge_lots == 2
    assert eng.position.fees_rub == 20
    second = eng.close_partial("2026-10-02", 110, 990, 0, 2, "exit", 20, True)
    assert second.net_pnl_rub == 160
    assert eng.position is None


def st5_session(tmp_path, monkeypatch, execution):
    from app.st5.service import St5Session
    from app.st5.models import St5Position, St5State
    s = St5Session()
    s._session_file = tmp_path / "st5.json"
    s.cfg.notify.notify_exit = False
    s.cfg.notify.notify_errors = False
    s.cfg.connector.account_id = "account"
    s.state["sandbox_active"] = True
    eng = s.engines["sber"]
    eng.position = St5Position("sber", St5State.LONG_SPREAD, 1, -2, 0, 1,
                              5, 5, 100, 100, 10, ord_lots=3, units=1,
                              unit_ord=3, unit_pref=5, fees_rub=8,
                              ord_entry_fee=3, pref_entry_fee=5,
                              broker_accounting=True, accounting_complete=True)
    def close(*a, **kw):
        raise RuntimeError("частичное исполнение / ошибка сети")
    ex = SimpleNamespace(close_pair=close, last_execution=execution)
    monkeypatch.setattr(s, "_make_executor", lambda pid: ex)
    return s, eng


def test_st5_failed_exit_records_only_confirmed_legs(tmp_path, monkeypatch):
    execution = {"ord": {"lots": 0, "price": None, "fee": 0},
                 "pref": {"lots": 2, "price": 110, "fee": 2}}
    s, eng = st5_session(tmp_path, monkeypatch, execution)
    tr = eng._close(2, 0, 0, 100, 110, "exit")
    s._on_engine_trade("sber", eng, tr, 100, 110)
    assert eng.position.lots == 3 and eng._pos_ord_lots() == 3
    assert len(s.trades) == 1
    assert s.trades[0]["lots"] == 2 and s.trades[0]["ord_lots"] == 0
    assert s.trades[0]["net_pnl_rub"] == 16
    assert s.trades[0]["accounting_complete"] is True
    assert s.pending_executions == {}


def test_st5_zero_ordinary_residual_is_not_legacy_equal_legs(tmp_path, monkeypatch):
    s, eng = st5_session(tmp_path, monkeypatch, {
        "ord": {"lots": 3, "price": 100, "fee": 3},
        "pref": {"lots": 0, "price": None, "fee": 0}})
    tr = eng._close(2, 0, 0, 100, 110, "exit")
    s._on_engine_trade("sber", eng, tr, 100, 110)
    assert eng.position.lots == 5
    assert eng._pos_ord_lots() == 0
    assert s.snapshot()["positions"][0]["ord_lots"] == 0


def test_st5_unknown_exit_never_creates_profit_or_repeats(tmp_path, monkeypatch):
    s, eng = st5_session(tmp_path, monkeypatch, {
        "ord": {"lots": None}, "pref": {"lots": 2, "price": 110, "fee": 2}})
    tr = eng._close(2, 0, 0, 100, 110, "exit")
    s._on_engine_trade("sber", eng, tr, 100, 110)
    assert s.trades == [] and eng.trades == []
    assert eng.position.lots == 5 and eng._pos_ord_lots() == 3
    assert "sber" in s.pending_executions
    tr2 = eng._close(3, 0, 0, 100, 110, "flat_all")
    monkeypatch.setattr(s, "_make_executor", lambda pid: pytest.fail("повтор неизвестного ордера"))
    s._on_engine_trade("sber", eng, tr2, 100, 110)
    assert eng.position.lots == 5 and s.trades == []
    from app.st5.service import St5Session
    restored = St5Session()
    restored._session_file = s._session_file
    assert restored.load_session()
    assert "sber" in restored.pending_executions


def test_st9_fact_pnl_from_weighted_retries_and_real_fees(tmp_path, monkeypatch):
    from app.st9.service import St9Session
    from app.st9.engine import St9Engine
    s = St9Session()
    s._session_file = tmp_path / "st9.json"
    eng = St9Engine("IMOEXF", 20, 10, 3, 14, pv=10, fee_per_lot=0)
    eng.open("long", 100, 5, 1, 1)
    s.engines[eng.secid] = eng
    s._entry_fill_px[eng.secid] = 101
    s._entry_fill_fee[eng.secid] = 5
    responses = iter([(2, 108, 2), (3, 112, 3)])
    def order(*a, **kw):
        qty, s._last_fill_px, s._last_order_fee = next(responses)
        return qty
    monkeypatch.setattr(s, "_order", order)
    assert s._close_order("IMOEXF", 5, "SELL", 110) == 5
    assert s._last_fill_px == pytest.approx(110.4)
    tr = eng.close(110, 2, "exit")
    rec = s._record_trade(eng, tr, 5)
    assert rec["gross_pnl_rub"] == 500
    assert rec["actual_gross_pnl_rub"] == pytest.approx(470)
    assert rec["actual_fees_rub"] == 10
    assert rec["actual_net_pnl_rub"] == pytest.approx(460)
    assert s.snapshot()["net_pnl_rub"] == 460
    assert s.snapshot()["model_net_pnl_rub"] == 495  # модельная комиссия 0.05% за сторону


def test_st9_partial_fee_allocation_and_missing_fact(tmp_path):
    from app.st9.service import St9Session
    from app.st9.engine import St9Engine
    s = St9Session()
    eng = St9Engine("IMOEXF", 20, 10, 3, 14, pv=10, fee_per_lot=0)
    eng.open("long", 100, 5, 1, 1)
    s._entry_fill_px["IMOEXF"] = 100
    s._entry_fill_fee["IMOEXF"] = 5
    s._last_fill_px, s._last_order_fee = 110, 2
    rec = s._record_trade(eng, eng.close_partial(110, 2, 2, "partial"), 5)
    assert rec["actual_net_pnl_rub"] == 196
    assert s._entry_fill_fee["IMOEXF"] == 3
    s._last_order_fee = None
    rec = s._record_trade(eng, eng.close(110, 3, "exit"), 3)
    assert rec["actual_net_pnl_rub"] is None
    assert "IMOEXF" not in s._entry_fill_fee


def test_st5_repeated_execution_does_not_send_second_order(tmp_path, monkeypatch):
    s, eng = st5_session(tmp_path, monkeypatch, {
        "ord": {"lots": 3, "price": 100, "fee": 3},
        "pref": {"lots": 5, "price": 110, "fee": 5}})
    tr = eng._close(2, 0, 0, 100, 110, "exit")
    s._on_engine_trade("sber", eng, tr, 100, 110)
    assert eng.position is None and len(s.trades) == 1
    monkeypatch.setattr(s, "_make_executor", lambda pid: pytest.fail("повтор исполненной сделки"))
    s._on_engine_trade("sber", eng, tr, 100, 110)
    assert len(s.trades) == 1


def test_st5_cannot_send_exit_if_checkpoint_write_fails(tmp_path, monkeypatch):
    s, eng = st5_session(tmp_path, monkeypatch, {})
    tr = eng._close(2, 0, 0, 100, 110, "exit")
    monkeypatch.setattr(s, "save_session", lambda: False)
    monkeypatch.setattr(s, "_make_executor", lambda pid: SimpleNamespace(
        close_pair=lambda *a, **kw: pytest.fail("ордер без сохранённого состояния")))
    s._on_engine_trade("sber", eng, tr, 100, 110)
    assert eng.position.lots == 5 and s.trades == []


def test_st8_futures_response_price_and_fee_used(tmp_path, monkeypatch):
    from app.st8.executor import St8Executor
    from app.st4 import tbank_sandbox as sb
    ex = St8Executor("account", paper=False)
    instrument = {"uid": "uid", "basicAssetSize": money(10),
                  "minPriceIncrement": money(0.5), "minPriceIncrementAmount": money(5)}
    ex._share_cache["TTZ6"] = instrument
    monkeypatch.setattr(sb, "post_order", lambda *a, **kw: {
        "lotsExecuted": 3, "executedOrderPrice": money(3010),
        "executedCommission": money(4.515), "orderId": "entry"})
    result = ex.open("TATN", 3, 300, 0, 0, fut_secid="TTZ6")
    assert result["stock_fill"]["price"] == 301
    assert result["stock_fill"]["fee"] == pytest.approx(4.515)
    from app.st8.service import St8Session
    args = St8Session._entry_accounting(result, 300, 0)
    assert args["stock_px"] == 301 and args["accounting_complete"]


def test_st9_ledger_includes_negative_varmargin(monkeypatch):
    from app.st9.service import St9Session
    from app.st4 import tbank_sandbox as sb
    s = St9Session()
    s.cfg.mode, s.cfg.account_id = "tbank_sandbox", "account"
    monkeypatch.setattr(sb, "operations", lambda *a: [
        {"id": "a", "operationType": "OPERATION_TYPE_ACCRUING_VARMARGIN", "payment": money(100)},
        {"id": "b", "operationType": "OPERATION_TYPE_WRITING_OFF_VARMARGIN", "payment": money(-80)}])
    monkeypatch.setattr(sb, "free_money_rub", lambda *a: 1000)
    assert s.ledger()["varmargin_rub"] == 20


def test_daily_reconcile_uses_requested_moscow_day_and_real_account(monkeypatch):
    import datetime
    from app import api
    from app.st4 import tbank_live as live
    msk = datetime.timezone(datetime.timedelta(hours=3))
    monkeypatch.setattr(api.ST5.cfg.connector, "mode", "paper")
    monkeypatch.setattr(api.ST8.cfg, "mode", "paper")
    monkeypatch.setattr(api.ST9.cfg, "mode", "tbank_real")
    monkeypatch.setattr(api.ST9.cfg, "account_id", "account")
    monkeypatch.setattr(api.ST9, "trades", [{"entry_ts": 0,
        "exit_ts": int(datetime.datetime(2026, 8, 13, 12, tzinfo=msk).timestamp() * 1000),
        "fees_rub": 50, "actual_fees_rub": 75, "actual_entry_fees_rub": 0}])
    monkeypatch.setattr(api.ST9, "engines", {})
    calls = []
    def operations(account, start, end):
        calls.append((account, start, end))
        return [{"id": "fee", "date": "2026-08-13T09:00:00Z",
                 "operationType": "OPERATION_TYPE_BROKER_FEE", "payment": money(-75)}]
    monkeypatch.setattr(live, "operations", operations)
    row = next(r for r in api._daily_ledger_recon("2026-08-13", msk) if r[0].startswith("ST9"))
    assert calls == [("account", "2026-08-12T21:00:00+00:00", "2026-08-13T21:00:00+00:00")]
    assert row[2] == 75 and "Δ+0" in row[1]


def test_st8_partial_entry_fees_survive_restart(tmp_path, monkeypatch):
    from app.st8.service import St8Session
    s, eng = st8_session(tmp_path, monkeypatch)
    result = {"stock_closed": 2, "hedge_closed": 0,
              "stock_fill": {"price": 310, "fee": 2}}
    s._record_close(eng, result, "2026-10-02", 310, 0, "partial")
    s.save_session()
    restored = St8Session()
    restored._session_file = s._session_file
    assert restored.load_session()
    p = restored.engines["TATN"].position
    assert p.lots == 3 and p.fees_rub == 3 and p.stock_entry_fee == 3
    assert p.accounting_complete


def test_st9_entry_fill_fee_survives_restart(tmp_path):
    from app.st9.service import St9Session
    from app.st9.engine import St9Engine
    s = St9Session()
    s._session_file = tmp_path / "st9.json"
    eng = St9Engine("IMOEXF", 20, 10, 3, 14, pv=10, fee_per_lot=0)
    eng.open("long", 100, 3, 1, 1)
    s.engines["IMOEXF"] = eng
    s._entry_fill_px["IMOEXF"] = 101
    s._entry_fill_fee["IMOEXF"] = 3.25
    s.save_session()
    restored = St9Session()
    restored._session_file = s._session_file
    assert restored.load_session()
    assert restored._entry_fill_fee["IMOEXF"] == 3.25
    assert restored._entry_fill_px["IMOEXF"] == 101
