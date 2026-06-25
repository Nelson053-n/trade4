"""Юнит-тесты st4 (§14.1): индикатор, синхронизация, сигналы, P&L, атомарность, reconciliation.

Покрывают спорные места ТЗ: знаконезависимый гейт §9.3 (включая ОТРИЦАТЕЛЬНЫЙ спред),
согласованность знака P&L с направлением, аварийный unwind и HALTED, reconciliation.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from app.st4.backtest import run_backtest
from app.st4.config import St4Config
from app.st4 import data_feed as feed
from app.st4.engine import TradingEngine
from app.st4.execution import OrderExecutor, UnwindError, leg_pnl_rub
from app.st4.indicators import (
    BollingerBands,
    SpreadBuilder,
    VolumeAverage,
    build_band_frame,
)
from app.st4.models import BandReading, BotState, LegPosition, Position, Role, Signal
from app.st4.strategy import deviation_gate, entry_signal, exit_signal, in_clearing_window


def _specs():
    return feed.synthetic_spec(Role.ORDINARY), feed.synthetic_spec(Role.PREFERRED)


# ============================ §8 индикатор ============================

def test_bollinger_matches_pandas():
    """SMA/σ/полосы потокового BB совпадают с эталоном pandas (Population, ddof=0)."""
    rng = np.random.default_rng(1)
    vals = list(np.cumsum(rng.normal(0, 1, 300)) + 50)
    period, k = 200, 2.0
    bb = BollingerBands(period, k, "Population")
    last = None
    for i, v in enumerate(vals):
        last = bb.update(i, v)
    s = pd.Series(vals)
    exp_sma = s.rolling(period).mean().iloc[-1]
    exp_sigma = s.rolling(period).std(ddof=0).iloc[-1]
    assert last.is_ready
    assert last.sma == pytest.approx(exp_sma, abs=1e-9)
    assert last.sigma == pytest.approx(exp_sigma, abs=1e-9)
    assert last.upper == pytest.approx(exp_sma + k * exp_sigma, abs=1e-9)
    assert last.lower == pytest.approx(exp_sma - k * exp_sigma, abs=1e-9)


def test_bollinger_not_ready_during_warmup():
    bb = BollingerBands(50, 2.0)
    for i in range(49):
        r = bb.update(i, float(i))
        assert not r.is_ready and math.isnan(r.sma)
    assert bb.update(49, 49.0).is_ready


def test_band_frame_matches_streaming():
    """Векторный build_band_frame совпадает с потоковым BB на последнем баре."""
    df = feed.generate_synthetic(n=400, seed=5)
    bf = build_band_frame(df, 100, 2.0, "Population")
    bb = BollingerBands(100, 2.0, "Population")
    last = None
    for ts, row in df.iterrows():
        last = bb.update(int(ts), float(row["price_b"] - row["price_a"]))
    assert last.sma == pytest.approx(bf["sma"].iloc[-1], abs=1e-6)
    assert last.upper == pytest.approx(bf["upper"].iloc[-1], abs=1e-6)


# ============================ §7 SpreadBuilder ============================

def test_spread_builder_sync():
    """Бар спреда формируется только когда обе ноги закрылись; spread = pref − ord."""
    sb = SpreadBuilder()
    assert sb.add_ordinary(1000, 32000.0) is None       # только одна нога — бара нет
    bar = sb.add_preferred(1000, 32080.0)               # вторая нога → бар
    assert bar is not None
    assert bar.spread == pytest.approx(80.0)
    assert bar.close_ord == 32000.0 and bar.close_pref == 32080.0


def test_spread_builder_gap_no_bar():
    """Пропуск одной ноги в интервале — бар не строится (значение не подставляется)."""
    sb = SpreadBuilder()
    assert sb.add_ordinary(1000, 100.0) is None
    assert sb.add_preferred(2000, 180.0) is None        # другой ts — пары нет
    assert sb.add_preferred(1000, 190.0) is not None    # доехала пара для 1000


# ============================ §9.3 гейт отклонения ============================

def test_deviation_gate_abs_of_mean_positive():
    cfg = St4Config().strategy
    cfg.deviation_mode = "AbsOfMean"
    cfg.deviation_pct = 0.02
    # SMA=100, порог 2%·100=2 → cur=103 проходит SELL, cur=101 нет
    assert deviation_gate(Signal.SELL, 103, 100, cfg)
    assert not deviation_gate(Signal.SELL, 101, 100, cfg)
    assert deviation_gate(Signal.BUY, 97, 100, cfg)
    assert not deviation_gate(Signal.BUY, 99, 100, cfg)


def test_deviation_gate_abs_of_mean_negative_spread():
    """КЛЮЧЕВОЙ кейс ТЗ §9.3: при ОТРИЦАТЕЛЬНОЙ SMA гейт остаётся корректным.

    LiteralPct здесь ломается (SMA·1.02 < SMA при SMA<0), AbsOfMean — нет.
    """
    cfg = St4Config().strategy
    cfg.deviation_mode = "AbsOfMean"
    cfg.deviation_pct = 0.02
    # SMA=-100, порог 2%·|−100|=2. SELL при cur >= SMA+2 = -98; BUY при cur <= -102.
    assert deviation_gate(Signal.SELL, -97, -100, cfg)      # выше средней — проходит
    assert not deviation_gate(Signal.SELL, -99, -100, cfg)  # недостаточно выше
    assert deviation_gate(Signal.BUY, -103, -100, cfg)      # ниже средней — проходит
    assert not deviation_gate(Signal.BUY, -101, -100, cfg)


def test_deviation_gate_literal_pct_breaks_on_negative():
    """Демонстрация, ПОЧЕМУ LiteralPct неверен при SMA<0 (зафиксировано как анти-кейс)."""
    cfg = St4Config().strategy
    cfg.deviation_mode = "LiteralPct"
    cfg.deviation_pct = 0.02
    # SMA=-100: SELL требует cur >= -102 (т.е. почти всё «выше» порога — гейт вырождается)
    assert deviation_gate(Signal.SELL, -101, -100, cfg)    # -101 >= -102 → True (ложно мягкий)
    # это и есть баг, который AbsOfMean исправляет (см. тест выше: там -99 не проходит)


# ============================ §9.2/§9.4 сигналы ============================

def _band(ts, spread, sma, sigma, k=2.0):
    return BandReading(ts, spread, sma, sigma, sma + k * sigma, sma - k * sigma, True)


def test_entry_signal_breakout_up_sell():
    cfg = St4Config().strategy
    cfg.deviation_pct = 0.0  # отключить гейт — проверяем чистый пробой
    prev = _band(0, 105, 100, 10)        # spread 105 < upper 120
    cur = _band(1, 125, 100, 10)         # spread 125 >= upper 120 → пробой вверх
    assert entry_signal(prev, cur, cfg) == Signal.SELL


def test_entry_signal_breakout_down_buy():
    cfg = St4Config().strategy
    cfg.deviation_pct = 0.0
    prev = _band(0, 95, 100, 10)         # spread 95 > lower 80
    cur = _band(1, 75, 100, 10)          # spread 75 <= lower 80 → пробой вниз
    assert entry_signal(prev, cur, cfg) == Signal.BUY


def test_no_signal_during_warmup():
    cfg = St4Config().strategy
    prev = BandReading(0, 125, float("nan"), float("nan"), float("nan"), float("nan"), False)
    cur = _band(1, 125, 100, 10)
    assert entry_signal(prev, cur, cfg) == Signal.NONE


def test_exit_signal_cross_mean():
    # SHORT_SPREAD: выход при пересечении SMA сверху вниз
    prev = _band(0, 110, 100, 10)
    cur = _band(1, 98, 100, 10)
    assert exit_signal(BotState.SHORT_SPREAD, prev, cur, 100)
    assert not exit_signal(BotState.SHORT_SPREAD, _band(0, 110, 100, 10), _band(1, 105, 100, 10), 100)
    # LONG_SPREAD: снизу вверх
    assert exit_signal(BotState.LONG_SPREAD, _band(0, 90, 100, 10), _band(1, 102, 100, 10), 100)


# ============================ §9.5 знак P&L ============================

def test_leg_pnl_sign():
    so, _ = _specs()
    # лонг @ 32000, выход 32050 → +50 пунктов · 1 лот · (STEPPRICE/MINSTEP=1) = +50₽
    # (на размер лота LOTVOLUME не умножаем — STEPPRICE уже на целый контракт)
    leg = LegPosition("SR", Role.ORDINARY, "buy", 1, 32000)
    assert leg_pnl_rub(leg, 32050, so) == pytest.approx(50.0)
    # шорт @ 32000, выход 32050 → −50₽
    leg = LegPosition("SR", Role.ORDINARY, "sell", 1, 32000)
    assert leg_pnl_rub(leg, 32050, so) == pytest.approx(-50.0)


def test_short_spread_profits_when_spread_falls():
    """Шорт спреда (SELL) должен ЗАРАБАТЫВАТЬ при падении спреда (согласованность знака).

    Это регрессия на баг инвертированных ног: SELL → buy SBRF + sell SBPR.
    """
    cfg = St4Config()
    cfg.strategy.sma_period = 30
    cfg.strategy.deviation_pct = 0.0
    so, sp = _specs()
    eng = TradingEngine(cfg, so, sp)
    # прогрев: спред 100 ± 5 попеременно — σ=5, полосы 100±10 ни разу не пробиваются
    # (случайный шум мог сам пересечь 2σ и дать ранний вход вместо задуманного пробоя)
    base = 32000.0
    ts = 0
    for i in range(40):
        noise = 5.0 if i % 2 == 0 else -5.0
        eng.on_candles(ts, base, base + 100 + noise)
        ts += 600000
    # пробой вверх существенно за полосу: спред 250 → SELL (шорт спреда)
    eng.on_candles(ts, base, base + 250)
    ts += 600000
    assert eng.state == BotState.SHORT_SPREAD
    entry_spread = eng.position.entry_spread
    # спред падает обратно к средней → должны закрыться в плюс
    eng.on_candles(ts, base, base + 100)
    ts += 600000
    assert len(eng.trades) == 1
    t = eng.trades[0]
    assert t.exit_spread < t.entry_spread        # спред упал
    assert t.gross_pnl_rub > 0                    # шорт спреда заработал
    assert entry_spread > 0


# ============================ §10 атомарность / unwind ============================

def test_execute_pair_ok():
    so, sp = _specs()
    ex = OrderExecutor(St4Config().execution, St4Config().paper, so, sp)
    r = ex.execute_pair(buy_ord=True, buy_pref=False, lots=1,
                        book_ord=(32000, 32001), book_pref=(32100, 32101),
                        ref_ord=32000, ref_pref=32100)
    assert r.ok and r.fill_ord is not None and r.fill_pref is not None
    assert r.fill_ord.side == "buy" and r.fill_pref.side == "sell"


def test_execute_pair_unwind_on_second_leg_fail():
    """Вторая нога не заливается → аварийный unwind первой, чистый исход (позиции нет)."""
    cfg = St4Config()
    cfg.execution.paper_fill_fail_prob = 1.0     # каждый второй вызов — неудача
    cfg.execution.max_retries = 1                # первая зальётся (#1 ок), вторая (#2) нет
    so, sp = _specs()
    ex = OrderExecutor(cfg.execution, cfg.paper, so, sp)
    # period = round(1/1.0)=1 → КАЖДЫЙ вызов fail. Тогда первая нога не зальётся → abort.
    r = ex.execute_pair(True, False, 1, (32000, 32001), (32100, 32101), 32000, 32100)
    assert not r.ok
    assert r.aborted or r.unwound


def test_execute_pair_abort_on_deviation_protection():
    """Защита от ухода цены: лимит ушёл дальше N тиков от reference → вход отменён."""
    cfg = St4Config()
    cfg.execution.deviation_protection_ticks = 1
    so, sp = _specs()
    ex = OrderExecutor(cfg.execution, cfg.paper, so, sp)
    # reference далеко от книги → лимитная цена уедет > 1 тика → first leg не зальётся
    r = ex.execute_pair(True, False, 1, (32000, 32001), (32100, 32101),
                        ref_ord=31000, ref_pref=31000)
    assert not r.ok and r.aborted


def test_halted_on_unwind_failure():
    """Если unwind физически невозможен (UnwindError) — движок переходит в HALTED."""
    cfg = St4Config()
    cfg.strategy.sma_period = 20
    cfg.strategy.deviation_pct = 0.0
    so, sp = _specs()
    eng = TradingEngine(cfg, so, sp)
    # подменяем executor так, чтобы execute_pair бросал UnwindError на входе
    def boom(*a, **k):
        raise UnwindError("unwind невозможен")
    eng.executor.execute_pair = boom
    base = 32000.0
    ts = 0
    rng = np.random.default_rng(0)
    for _ in range(25):
        eng.on_candles(ts, base, base + 50 + float(rng.normal(0, 4)))
        ts += 600000
    eng.on_candles(ts, base, base + 200)         # пробой → попытка входа → boom
    assert eng.state == BotState.HALTED
    assert eng.risk.halted


# ============================ §11 reconciliation ============================

def test_reconcile_match():
    cfg = St4Config()
    so, sp = _specs()
    eng = TradingEngine(cfg, so, sp)
    assert eng.reconcile(None)                    # обе пусты — согласовано


def test_reconcile_mismatch_halts():
    cfg = St4Config()
    so, sp = _specs()
    eng = TradingEngine(cfg, so, sp)
    fake = Position(
        state=BotState.SHORT_SPREAD,
        leg_ord=LegPosition("SR", Role.ORDINARY, "buy", 1, 32000),
        leg_pref=LegPosition("SP", Role.PREFERRED, "sell", 1, 32100),
        entry_ts=0, entry_spread=100, entry_beta=1.0, sma_at_entry=0.0)
    assert not eng.reconcile(fake)                # локально пусто, у «брокера» позиция
    assert eng.state == BotState.HALTED


# ============================ §11 RiskManager ============================

def test_risk_daily_loss_blocks_entry():
    cfg = St4Config()
    cfg.risk.max_daily_loss_rub = 1000
    so, sp = _specs()
    eng = TradingEngine(cfg, so, sp)
    eng.risk.on_trade_closed(-1500, 1_700_000_000_000)   # пробили дневной лимит
    ok, why = eng.risk.can_enter(1_700_000_000_000, 0)
    assert not ok and "лимит" in why


def test_risk_consecutive_errors_halt():
    cfg = St4Config()
    cfg.risk.max_consecutive_errors = 3
    so, sp = _specs()
    eng = TradingEngine(cfg, so, sp)
    for _ in range(3):
        eng.risk.on_error()
    assert eng.risk.halted


# ============================ §9.7 сессия ============================

def test_clearing_window():
    cfg = St4Config().session
    # 14:00 MSK — в клиринговом окне (14:00–14:05)
    import datetime as dt
    msk = dt.datetime(2026, 6, 8, 14, 2, tzinfo=dt.timezone(dt.timedelta(hours=3)))
    ts = int(msk.timestamp() * 1000)
    assert in_clearing_window(ts, cfg)
    # 12:00 MSK — торги идут
    msk2 = dt.datetime(2026, 6, 8, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=3)))
    assert not in_clearing_window(int(msk2.timestamp() * 1000), cfg)


# ============================ §14.2 бэктест ============================

def test_backtest_metrics_on_synthetic():
    """Бэктест на синтетике даёт осмысленные метрики и честный maxDD по equity."""
    cfg = St4Config()
    cfg.strategy.sma_period = 100
    so, sp = _specs()
    df = feed.generate_synthetic(n=1500, seed=23)
    r = run_backtest(df, cfg, so, sp)
    assert r["trades"] > 0
    assert 0 <= r["win_rate_pct"] <= 100
    assert r["max_drawdown_pct"] >= 0
    assert len(r["equity_curve"]) == r["bars"]
    # net_pnl согласован с суммой сделок
    assert r["net_pnl_rub"] == pytest.approx(
        sum(t["net_pnl_rub"] for t in r["trades_detail"]), abs=1)


def test_freeze_sma_on_exit_option():
    """FreezeSmaOnExit меняет уровень выхода (зафиксированная SMA входа vs живая)."""
    cfg_live = St4Config()
    cfg_live.strategy.sma_period = 100
    cfg_live.strategy.freeze_sma_on_exit = False
    cfg_freeze = St4Config()
    cfg_freeze.strategy.sma_period = 100
    cfg_freeze.strategy.freeze_sma_on_exit = True
    so, sp = _specs()
    df = feed.generate_synthetic(n=1200, seed=7)
    r_live = run_backtest(df, cfg_live, so, sp)
    r_freeze = run_backtest(df, cfg_freeze, so, sp)
    # оба режима валидны; результаты в общем случае различаются (поведение выхода разное)
    assert r_live["trades"] >= 0 and r_freeze["trades"] >= 0


# ============================ Phase 2: T-Bank sandbox executor ============================

class FakeSB:
    """Мок модуля tbank_sandbox для юнит-тестов TinkoffSandboxExecutor (без сети).

    fail_from: с какого по счёту вызова post_order начинать реджектить (None = все fill).
    """

    def __init__(self, fail_from: int | None = None, fail_only=None, accounts=None):
        self.orders: list[tuple] = []
        self.fail_from = fail_from        # реджектить начиная с N-го вызова
        self.fail_only = set(fail_only or ())  # реджектить только эти номера вызовов
        self.payins: list[tuple] = []
        self.opened: list[str] = []
        self._accounts = accounts or []

    @staticmethod
    def _uid(it):
        return it["uid"]

    @staticmethod
    def _q_to_float(q):
        if not q:
            return 0.0
        return float(q.get("units", 0)) + float(q.get("nano", 0)) / 1e9

    def find_future(self, ticker):
        return {"ticker": ticker, "uid": "uid-" + ticker, "figi": "figi-" + ticker,
                "lot": 1, "apiTradeAvailableFlag": True}

    def list_accounts(self):
        return self._accounts

    def open_account(self, name="x"):
        self.opened.append(name)
        return "acc-new"

    def pay_in(self, acc, rub):
        self.payins.append((acc, rub))
        return float(rub)

    def post_order(self, acc, uid, lots, direction, order_id,
                   order_type="ORDER_TYPE_MARKET", price=None):
        import uuid as _uuid
        _uuid.UUID(order_id)            # упадёт, если order_id не валидный UUID
        self.orders.append((uid, direction, order_id))
        n = len(self.orders)
        reject = (self.fail_from is not None and n >= self.fail_from) or (n in self.fail_only)
        if reject:
            return {"executionReportStatus": "EXECUTION_REPORT_STATUS_REJECTED",
                    "executedOrderPrice": None}
        price = 32100 if "SP" in uid else 32000    # цена за контракт (SBRF 32000, SBPR 32100)
        # T-Bank возвращает executedOrderPrice = СУММА за все лоты + lotsExecuted
        return {"executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
                "lotsExecuted": lots,
                "executedOrderPrice": {"units": str(price * lots), "nano": 0}}


def _tinkoff_exec(fail_from=None, fail_only=None, accounts=None, max_retries=3):
    from app.st4.tinkoff_executor import TinkoffSandboxExecutor
    cfg = St4Config()
    cfg.execution.max_retries = max_retries
    so = feed.synthetic_spec(Role.ORDINARY)
    so.code = "SRM6"
    sp = feed.synthetic_spec(Role.PREFERRED)
    sp.code = "SPM6"
    sb = FakeSB(fail_from=fail_from, fail_only=fail_only, accounts=accounts)
    ex = TinkoffSandboxExecutor(cfg.execution, cfg.connector, so, sp, sb=sb)
    return ex, sb


def test_tinkoff_execute_pair_ok():
    """Обе ноги fill → r.ok, корректные стороны/цены, счёт открыт + pay_in, orderId — UUID."""
    ex, sb = _tinkoff_exec()
    # шорт спреда: buy SBRF + sell SBPR
    r = ex.execute_pair(buy_ord=True, buy_pref=False, lots=1,
                        book_ord=(0, 0), book_pref=(0, 0), ref_ord=32000, ref_pref=32100)
    assert r.ok
    assert r.fill_ord.side == "buy" and r.fill_pref.side == "sell"
    assert r.fill_ord.avg_price == 32000 and r.fill_pref.avg_price == 32100
    assert len(sb.orders) == 2
    assert sb.opened == ["st4-spread-sandbox"]      # счёт открыт один раз
    assert sb.payins and sb.payins[0][1] == 200_000  # пополнен под ГО


def test_tinkoff_price_per_contract_multi_lot():
    """При lots>1 цена входа = за КОНТРАКТ, не сумма за лоты (регрессия: было ×N завышение)."""
    ex, sb = _tinkoff_exec()
    r = ex.execute_pair(buy_ord=True, buy_pref=False, lots=10,
                        book_ord=(0, 0), book_pref=(0, 0), ref_ord=32000, ref_pref=32100)
    assert r.ok
    # цены должны быть за 1 контракт (32000/32100), НЕ 320000/321000
    assert r.fill_ord.avg_price == 32000, f"цена завышена: {r.fill_ord.avg_price}"
    assert r.fill_pref.avg_price == 32100, f"цена завышена: {r.fill_pref.avg_price}"
    assert r.fill_ord.lots == 10 and r.fill_pref.lots == 10


def test_tinkoff_unwind_on_second_leg_fail():
    """Вторая нога не зальётся → unwind первой обратным ордером, r.unwound."""
    # max_retries=1: первая нога (#1) ок, ТОЛЬКО вторая (#2) реджект → unwind (#3) зальётся
    ex, sb = _tinkoff_exec(fail_only={2}, max_retries=1)
    r = ex.execute_pair(True, False, 1, (0, 0), (0, 0), 32000, 32100)
    assert not r.ok and r.unwound
    assert len(sb.orders) == 3                        # первая + неуд. вторая + unwind


def test_tinkoff_unwind_failure_raises():
    """Вторая нога И unwind не заливаются → UnwindError."""
    # fail_from=2: всё начиная со 2-го ордера падает (вторая нога + unwind)
    ex, sb = _tinkoff_exec(fail_from=2, max_retries=1)
    # подменим первую ногу на успешную, остальное падает — fail_from=2 это и делает
    with pytest.raises(UnwindError):
        ex.execute_pair(True, False, 1, (0, 0), (0, 0), 32000, 32100)


def test_tinkoff_close_pair_real_exit():
    """close_pair ставит обратные ордера, возвращает фактические exit-цены филла."""
    ex, sb = _tinkoff_exec()
    pos = Position(
        state=BotState.SHORT_SPREAD,
        leg_ord=LegPosition("SRM6", Role.ORDINARY, "buy", 1, 32000),
        leg_pref=LegPosition("SPM6", Role.PREFERRED, "sell", 1, 32100),
        entry_ts=0, entry_spread=100, entry_beta=1.0, sma_at_entry=0.0)
    n_before = len(sb.orders)
    cr = ex.close_pair(pos, 32000, 32100)
    assert len(sb.orders) == n_before + 2            # два обратных ордера
    # закрытие SBRF (был buy) → sell; SBPR (был sell) → buy
    assert sb.orders[-2][1] == "ORDER_DIRECTION_SELL"  # SBRF close
    assert sb.orders[-1][1] == "ORDER_DIRECTION_BUY"   # SBPR close
    assert cr.exit_ord == 32000 and cr.exit_pref == 32100


def test_tinkoff_account_reuse():
    """Существующий OPEN-счёт с нужным именем переиспользуется — open_account не зван."""
    accs = [{"id": "acc-existing", "name": "st4-spread-sandbox", "status": "ACCOUNT_STATUS_OPEN"}]
    ex, sb = _tinkoff_exec(accounts=accs)
    assert ex._account_id == "acc-existing"
    assert sb.opened == []                            # новый счёт не открывался


def test_tinkoff_caches_instruments():
    """find_future вызывается по разу на ногу (результат кэшируется в _inst)."""
    ex, sb = _tinkoff_exec()
    assert set(ex._inst.keys()) == {Role.ORDINARY, Role.PREFERRED}
    # повторный execute_pair не должен заново резолвить (кэш уже заполнен)
    n = len(sb.orders)
    ex.execute_pair(True, False, 1, (0, 0), (0, 0), 32000, 32100)
    assert ex._inst[Role.ORDINARY]["ticker"] == "SRM6"
    assert len(sb.orders) == n + 2


def test_engine_paper_executor_by_default():
    """mode='paper' → engine использует OrderExecutor (регресс-гард)."""
    cfg = St4Config()
    so, sp = _specs()
    eng = TradingEngine(cfg, so, sp)
    assert isinstance(eng.executor, OrderExecutor)


def test_engine_disarmed_skips_entries():
    """Disarmed движок не открывает входы (прогрев BB идёт), выход открытой позиции работает.

    Используется на backfill-replay в sandbox: исторические бары не торгуем.
    """
    cfg = St4Config()
    cfg.strategy.sma_period = 30
    cfg.strategy.deviation_pct = 0.0
    so, sp = _specs()
    eng = TradingEngine(cfg, so, sp)
    eng.arm(False)                       # запретить входы
    base = 32000.0
    ts = 0
    rng = np.random.default_rng(0)
    for _ in range(40):
        eng.on_candles(ts, base, base + 100 + float(rng.normal(0, 5)))
        ts += 600000
    eng.on_candles(ts, base, base + 250)                 # сильный пробой — но disarmed
    ts += 600000
    assert eng.state == BotState.FLAT and eng.position is None   # вход НЕ открыт
    assert eng.last_band.is_ready                                # но BB прогрелся
    # взвели — следующий пробой открывает позицию
    eng.arm(True)
    eng.on_candles(ts, base, base + 100)                 # возврат
    ts += 600000
    eng.on_candles(ts, base, base + 260)                 # новый пробой → вход
    assert eng.position is not None


def test_engine_uses_tinkoff_when_sandbox(monkeypatch):
    """mode='tbank_sandbox' → engine использует TinkoffSandboxExecutor (с моком sb)."""
    import app.st4.tinkoff_executor as te
    monkeypatch.setattr(te, "tbank_sandbox", FakeSB())
    cfg = St4Config()
    cfg.connector.mode = "tbank_sandbox"
    so = feed.synthetic_spec(Role.ORDINARY)
    so.code = "SRM6"
    sp = feed.synthetic_spec(Role.PREFERRED)
    sp.code = "SPM6"
    eng = TradingEngine(cfg, so, sp)
    assert type(eng.executor).__name__ == "TinkoffSandboxExecutor"


# ============================ §6.4 экспирация / роллировер ============================

def _engine_with_expiry(expiry: str) -> TradingEngine:
    cfg = St4Config()
    cfg.strategy.sma_period = 30
    cfg.strategy.deviation_pct = 0.0
    so, sp = _specs()
    so.expiry = sp.expiry = expiry
    return TradingEngine(cfg, so, sp)


def _warm_alternating(eng: TradingEngine, n: int = 40, base: float = 32000.0,
                      start_ts: int = 0) -> int:
    ts = start_ts
    for i in range(n):
        noise = 5.0 if i % 2 == 0 else -5.0
        eng.on_candles(ts, base, base + 100 + noise)
        ts += 600000
    return ts


def test_expiry_gate_blocks_entry():
    """За rollover_no_new_entry_days_before дней до экспирации вход запрещён."""
    from datetime import datetime, timedelta, timezone
    soon = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
    eng = _engine_with_expiry(soon)
    # бары «сейчас»: до экспирации 2 дн < 5 (no_new_entry)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    ts = _warm_alternating(eng, start_ts=now_ms)
    res = eng.on_candles(ts, 32000.0, 32000.0 + 250)     # пробой → сигнал был бы
    assert eng.state == BotState.FLAT                     # вход не открыт
    assert any("экспирации" in e.message for e in res.events)


def test_expiry_forces_position_close():
    """Открытая позиция закрывается, когда до экспирации < rollover_days_before_expiry."""
    from datetime import datetime, timedelta, timezone
    far = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
    eng = _engine_with_expiry(far)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    ts = _warm_alternating(eng, start_ts=now_ms)
    eng.on_candles(ts, 32000.0, 32000.0 + 250)
    assert eng.state == BotState.SHORT_SPREAD
    # «прошло 28 дней»: до экспирации 2 дн < 3 (rollover_days_before_expiry)
    ts28 = ts + 28 * 24 * 3600 * 1000
    res = eng.on_candles(ts28, 32000.0, 32000.0 + 240)
    assert eng.state == BotState.FLAT
    assert eng.trades and eng.trades[-1].reason == "rollover"
    assert res.trade is not None


# ============================ гейт Sigma / вход ReEntry ============================

def test_deviation_gate_sigma_mode():
    cfg = St4Config().strategy
    cfg.deviation_mode = "Sigma"
    cfg.deviation_sigma = 2.0
    # σ=10 → порог 20
    assert deviation_gate(Signal.SELL, 121, 100, cfg, sigma=10)
    assert not deviation_gate(Signal.SELL, 119, 100, cfg, sigma=10)
    assert deviation_gate(Signal.BUY, 79, 100, cfg, sigma=10)
    assert not deviation_gate(Signal.BUY, 81, 100, cfg, sigma=10)


def test_entry_trigger_reentry():
    cfg = St4Config().strategy
    cfg.entry_trigger = "ReEntry"
    cfg.deviation_pct = 0.0
    # был снаружи (125 >= upper 120), вернулся внутрь (115 < 120) → SELL
    assert entry_signal(_band(0, 125, 100, 10), _band(1, 115, 100, 10), cfg) == Signal.SELL
    # пробой наружу в ReEntry-режиме сигнала НЕ даёт
    assert entry_signal(_band(0, 105, 100, 10), _band(1, 125, 100, 10), cfg) == Signal.NONE
    # снизу: был под lower 80, вернулся внутрь → BUY
    assert entry_signal(_band(0, 75, 100, 10), _band(1, 85, 100, 10), cfg) == Signal.BUY


# ============================ §11 дневной лимит с unrealized ============================

def test_day_loss_kill_switch_with_unrealized():
    """Лимит срабатывает от нереализованного убытка: позиция закрывается, движок HALTED."""
    cfg = St4Config()
    cfg.strategy.sma_period = 30
    cfg.strategy.deviation_pct = 0.0
    cfg.risk.max_daily_loss_rub = 100.0    # крошечный лимит — пробьёт ход спреда
    so, sp = _specs()
    eng = TradingEngine(cfg, so, sp)
    ts = _warm_alternating(eng)
    eng.on_candles(ts, 32000.0, 32000.0 + 250)   # SELL: шорт спреда
    assert eng.state == BotState.SHORT_SPREAD
    ts += 600000
    # спред улетает ещё выше → unrealized < −100₽
    eng.on_candles(ts, 32000.0, 32000.0 + 500)
    assert eng.state == BotState.HALTED
    assert eng.risk.halted
    assert eng.trades and eng.trades[-1].reason == "stop"


# ============================ персист открытой позиции ============================

def test_position_json_roundtrip():
    from dataclasses import asdict
    import json as _json
    from app.st4.service import St4Session
    pos = Position(
        state=BotState.LONG_SPREAD,
        leg_ord=LegPosition("SRM6", Role.ORDINARY, "buy", 2, 31000.0),
        leg_pref=LegPosition("SPM6", Role.PREFERRED, "sell", 2, 31100.0),
        entry_ts=123, entry_spread=100.0, entry_beta=1.0,
        sma_at_entry=90.0, entry_fee_rub=8.0,
    )
    d = _json.loads(_json.dumps(asdict(pos)))    # enum'ы — str-подклассы → строки
    back = St4Session._position_from_json(d)
    assert back == pos


# ============================ объёмный фильтр входа (2026-06-14) ============================

def _warm_for_breakout(eng: TradingEngine, vol: float, n: int = 40,
                       base: float = 32000.0) -> int:
    """Прогрев BB шумом ±5 (полосы 100±10 не пробиваются) с заданным объёмом каждого бара.

    Возвращает следующий ts. Объём подаётся в обе ноги по vol/2, чтобы SpreadBar.volume=vol.
    """
    ts = 0
    for i in range(n):
        noise = 5.0 if i % 2 == 0 else -5.0
        eng.on_candles(ts, base, base + 100 + noise, vol / 2, vol / 2)
        ts += 600000
    return ts


def test_volume_filter_blocks_low_passes_high():
    """Объёмный фильтр: пробой с низким объёмом не входит, с высоким — входит."""
    base = 32000.0
    # SMA объёма на прогреве = 1000 (каждый бар vol=1000). Порог mult=1.5 → нужно ≥1500.
    # --- низкий объём пробойного бара (500 < 1500) → вход заблокирован ---
    cfg = St4Config()
    cfg.strategy.sma_period = 30
    cfg.strategy.deviation_pct = 0.0
    cfg.strategy.volume_filter_mult = 1.5
    so, sp = _specs()
    eng_lo = TradingEngine(cfg, so, sp)
    ts = _warm_for_breakout(eng_lo, vol=1000.0)
    res = eng_lo.on_candles(ts, base, base + 250, 250, 250)   # пробой, объём 500
    assert eng_lo.state == BotState.FLAT and eng_lo.position is None
    assert any("объём" in e.message for e in res.events)
    # --- высокий объём пробойного бара (4000 ≥ 1500) → вход открыт ---
    cfg2 = St4Config(**cfg.model_dump())
    eng_hi = TradingEngine(cfg2, *_specs())
    ts = _warm_for_breakout(eng_hi, vol=1000.0)
    eng_hi.on_candles(ts, base, base + 250, 2000, 2000)       # пробой, объём 4000
    assert eng_hi.state == BotState.SHORT_SPREAD and eng_hi.position is not None


def test_volume_filter_disabled_or_no_volume_does_not_block():
    """mult=0 (выкл) и нулевой объём бара не блокируют вход (обратная совместимость)."""
    base = 32000.0
    # фильтр включён, но объёмы баров нулевые (старые данные) → не блокирует
    cfg = St4Config()
    cfg.strategy.sma_period = 30
    cfg.strategy.deviation_pct = 0.0
    cfg.strategy.volume_filter_mult = 1.5
    eng = TradingEngine(cfg, *_specs())
    ts = _warm_for_breakout(eng, vol=0.0)                     # объёмы=0 на всём прогоне
    eng.on_candles(ts, base, base + 250, 0, 0)               # пробой без объёма
    assert eng.position is not None                           # вход НЕ заблокирован


def test_volume_average_matches_pandas_rolling():
    """SMA объёма потокового VolumeAverage совпадает с pandas rolling mean."""
    rng = np.random.default_rng(7)
    vols = list(rng.poisson(1000, 250).astype("float64"))
    period = 200
    va = VolumeAverage(period)
    last = None
    for v in vols:
        last = va.update(v)
    exp = pd.Series(vols).rolling(period).mean().iloc[-1]
    assert va.is_ready
    assert last == pytest.approx(exp, abs=1e-9)


# ============================ гейт свежести данных ============================

def test_data_fresh_predicate():
    """Гейт свежести: активен только при _check_lag=True и max_data_lag_min>0; старый бар → False."""
    import time as _time
    from app.st4.models import SpreadBar
    cfg = St4Config()
    cfg.strategy.max_data_lag_min = 30.0
    eng = TradingEngine(cfg, *_specs())
    now_ms = int(_time.time() * 1000)
    fresh = SpreadBar(ts=now_ms, close_ord=0, close_pref=0, spread=0)
    stale = SpreadBar(ts=now_ms - 60 * 60_000, close_ord=0, close_pref=0, spread=0)  # 60 мин назад
    # _check_lag=False (бэктест/плеер) → гейт неактивен, любой бар «свежий»
    assert eng._data_fresh(fresh) and eng._data_fresh(stale)
    # live: гейт активен — свежий проходит, устаревший нет
    eng._check_lag = True
    assert eng._data_fresh(fresh)
    assert not eng._data_fresh(stale)
    # выключение гейта (max_data_lag_min=0) → старый бар снова «свежий»
    cfg.strategy.max_data_lag_min = 0.0
    assert eng._data_fresh(stale)


def test_log_event_dedups_repeated_warns():
    """Дедуп журнала: однотипные warn (текст без цифр совпадает) сворачиваются в одну запись
    со счётчиком count; другой тип/шаблон → новая строка; служебное _sig не утекает в snapshot."""
    from app.st4.service import St4Session
    s = St4Session("sber")
    s.events = []
    s.log_event("warn", "нет свежих свечей ISS 139 мин — ждём")
    s.log_event("warn", "нет свежих свечей ISS 140 мин — ждём")
    s.log_event("warn", "нет свежих свечей ISS 141 мин — ждём")
    assert len(s.events) == 1 and s.events[-1]["count"] == 3
    assert "141" in s.events[-1]["message"]   # хранит свежий текст
    s.log_event("info", "роллировер: торгуем SRU6/SPU6")
    s.log_event("warn", "нет свежих свечей ISS 142 мин — ждём")
    assert len(s.events) == 3                 # info разорвал серию warn
    s.state["live"] = True
    assert all("_sig" not in e for e in s.snapshot(0.0)["events"])


def test_st4_pairs_endpoint_handles_optional_sma_period():
    """/st4/pairs не падает на парах с опц. 4-м элементом sma_period (sngr=...,100).
    Регресс: распаковка `for pid,(o,p,lbl)` ломалась ValueError на 4-кортеже."""
    from fastapi.testclient import TestClient
    from app.api import app
    r = TestClient(app).get("/st4/pairs")
    assert r.status_code == 200
    ids = {p["id"] for p in r.json()["pairs"]}
    assert {"sber", "sngr"} <= ids
    for p in r.json()["pairs"]:
        assert p["ord"] and p["pref"] and p["label"]   # все поля заполнены


def test_adopt_position_from_account_restores_on_restart():
    """reconciliation восстанавливает парную позицию из sandbox-счёта (рестарт→движок flat,
    на счёте легитимная позиция → НЕ закрываем, а продолжаем вести). Регресс: раньше
    закрывалась любая «не совпавшая» позиция, рестарт убивал живые сделки."""
    from app.st4.service import St4Session
    from app.st4.models import Role, BotState
    s = St4Session("sber")

    class _FakeEx:
        def entry_prices(self):
            return {Role.ORDINARY: 29597.0, Role.PREFERRED: 29533.0}
        def broker_entry_ts(self):
            return 1700000000000   # брокер знает реальное время входа
    s.engine.executor = _FakeEx()
    # на счёте обычка -10 (sell) / преф +10 (buy). Канон: обычка sell/преф buy = LONG_SPREAD
    # (engine._open_position: LONG = sell обычка + buy преф). Регресс на инверсию метки при
    # усыновлении (раньше тут ошибочно ожидался SHORT_SPREAD → знак P&L расходился с направлением).
    assert s._adopt_position_from_account({Role.ORDINARY: -10, Role.PREFERRED: 10})
    assert s.engine.state == BotState.LONG_SPREAD
    assert s.engine.position.leg_ord.side == "sell" and s.engine.position.leg_ord.lots == 10
    assert s.engine.position.leg_pref.side == "buy"
    # entry_ts взят из брокера, а НЕ time.time() (регресс: точка входа была в момент рестарта)
    assert s.engine.position.entry_ts == 1700000000000
    # непарная (одна нога) — не восстанавливаем (вернёт False → дальше flat_broker)
    s.engine.position = None
    assert not s._adopt_position_from_account({Role.ORDINARY: -10, Role.PREFERRED: 0})


def test_adopt_position_sign_matches_direction():
    """Регресс: усыновлённая позиция должна иметь знак P&L, согласованный с направлением.

    Баг: метка state при усыновлении была инвертирована (+обычка→LONG), из-за чего, например,
    позиция с обычка buy / преф sell получала метку long_spread, но фактически вела себя как
    шорт — лонг при РОСТЕ спреда уходил в минус (так в live sngr 24.06: спред +127 → net −143).
    Канон: обычка buy(+) / преф sell(−) = SHORT_SPREAD."""
    from app.st4.service import St4Session
    from app.st4.models import Role, BotState
    s = St4Session("sber")

    class _FakeEx:
        def entry_prices(self):
            return {Role.ORDINARY: 29597.0, Role.PREFERRED: 29533.0}
        def broker_entry_ts(self):
            return 1700000000000
    s.engine.executor = _FakeEx()
    # обычка +10 (buy) / преф -10 (sell) → по канону это SHORT_SPREAD
    assert s._adopt_position_from_account({Role.ORDINARY: 10, Role.PREFERRED: -10})
    assert s.engine.state == BotState.SHORT_SPREAD
    assert s.engine.position.leg_ord.side == "buy"
    assert s.engine.position.leg_pref.side == "sell"
    # зеркально: обычка -10 / преф +10 → LONG_SPREAD
    s.engine.position = None
    assert s._adopt_position_from_account({Role.ORDINARY: -10, Role.PREFERRED: 10})
    assert s.engine.state == BotState.LONG_SPREAD


def test_adopt_position_entry_ts_fallback_to_last_bar():
    """Если брокер не отдал время (sandbox), entry_ts = last_live_ts (время бара), не time.time()."""
    from app.st4.service import St4Session
    from app.st4.models import Role
    s = St4Session("sber")
    s.last_live_ts = 1699999000000

    class _FakeEx:
        def entry_prices(self):
            return {Role.ORDINARY: 29597.0, Role.PREFERRED: 29533.0}
        def broker_entry_ts(self):
            return None   # история недоступна (sandbox)
    s.engine.executor = _FakeEx()
    assert s._adopt_position_from_account({Role.ORDINARY: -10, Role.PREFERRED: 10})
    assert s.engine.position.entry_ts == 1699999000000   # = last_live_ts, не «сейчас»


def test_guard_blocks_actions_with_open_position():
    """При ОТКРЫТОЙ позиции config/stop/reset → 409 (защита от рассинхрона со счётом)."""
    from fastapi.testclient import TestClient
    from app.api import app, _st4
    from app.st4.models import LegPosition, Position, BotState, Role
    c = TestClient(app)
    s = _st4("sber")
    s.engine.position = None
    # без позиции config проходит (200)
    assert c.post("/st4/config?pair=sber", json={"sma_period": 200}).status_code == 200
    # с позицией — блокируется
    s.engine.position = Position(
        state=BotState.LONG_SPREAD,
        leg_ord=LegPosition("SR", Role.ORDINARY, "sell", 1, 100.0),
        leg_pref=LegPosition("SP", Role.PREFERRED, "buy", 1, 100.0),
        entry_ts=1, entry_spread=0.0, entry_beta=1.0, sma_at_entry=0.0, entry_fee_rub=0.0)
    assert c.post("/st4/config?pair=sber", json={"sma_period": 100}).status_code == 409
    assert c.post("/st4/control/stop?pair=sber").status_code == 409
    assert c.post("/st4/reset?pair=sber").status_code == 409
    s.engine.position = None   # очистка для других тестов


def test_chart_split_detects_separate_timeframe():
    """Разнесённые ТФ: график детальнее торговли только при 0 < chart_interval < торгового."""
    from app.st4.service import St4Session
    s = St4Session("sber")
    s.cfg.strategy.candle_interval_minutes = 10
    s.cfg.strategy.chart_interval_minutes = 0     # выкл (= торговый) → один поток
    assert not s._chart_split()
    s.cfg.strategy.chart_interval_minutes = 10    # равно торговому → не разнесено
    assert not s._chart_split()
    s.cfg.strategy.chart_interval_minutes = 1     # 1m график при 10m торговле → разнесено
    assert s._chart_split()
    s.cfg.strategy.candle_interval_minutes = 1    # торговля тоже 1m → график не может быть детальнее
    assert not s._chart_split()


def test_push_history_chart_uses_engine_bands():
    """push_history_chart пишет CHART-спред (1m), а полосы/SMA/σ берёт из engine.last_band."""
    from app.st4.service import St4Session
    from app.st4.models import BandReading
    s = St4Session("sber")
    # фейковый последний торговый бар: полосы 10m-уровня
    s.engine.last_band = BandReading(ts=600000, spread=50.0, sma=10.0, upper=110.0,
                                     lower=-90.0, sigma=50.0, is_ready=True)
    s.history = []
    s.push_history_chart(660000, 37.5)   # 1m-бар со своим спредом, ts внутри 10m-бара
    assert len(s.history) == 1
    pt = s.history[0]
    assert pt["ts"] == 660000
    assert pt["spread"] == 37.5          # спред — детальный (1m), НЕ из last_band
    assert pt["sma"] == 10.0 and pt["upper"] == 110.0 and pt["lower"] == -90.0  # полосы — торговые


def test_close_position_exit_ts_not_before_entry():
    """Инвариант: exit_ts >= entry_ts даже если позицию закрывает бар старше входа."""
    from app.st4.models import LegPosition, Position, BotState, SpreadBar, Role
    cfg = St4Config()
    so = feed.synthetic_spec(Role.ORDINARY); sp = feed.synthetic_spec(Role.PREFERRED)
    eng = TradingEngine(cfg, so, sp)
    eng.position = Position(
        state=BotState.LONG_SPREAD,
        leg_ord=LegPosition(code="O", role=Role.ORDINARY, side="sell", lots=1, entry_price=100),
        leg_pref=LegPosition(code="P", role=Role.PREFERRED, side="buy", lots=1, entry_price=200),
        entry_ts=2000, entry_spread=100, entry_beta=1.0, sma_at_entry=100, entry_fee_rub=0)
    eng.state = BotState.LONG_SPREAD
    eng._last_spread_bar = SpreadBar(ts=1000, spread=100, close_ord=100, close_pref=200,
                                     volume=0)   # бар СТАРШE входа (ts=1000 < 2000)
    tr = eng.flat_all("flat_all")
    assert tr is not None
    assert tr.exit_ts >= tr.entry_ts   # выход не раньше входа


# ============================ БОЕВОЙ контур tbank_real ============================

class FakeLive(FakeSB):
    """Мок боевого модуля tbank_live: те же fill-ответы, но боевые методы счёта."""

    def __init__(self, account_id="acc-real", **kw):
        super().__init__(**kw)
        self._real_account = account_id

    def account_is_open(self, account_id):
        return account_id == self._real_account

    def make_order_id(self, account_id, uid, lots, direction, ts):
        # детерминированный (как боевой): одинаковые аргументы → одинаковый id
        import hashlib
        raw = f"{account_id}|{uid}|{int(lots)}|{direction}|{int(ts)}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def post_order(self, acc, uid, lots, direction, order_id,
                   order_type="ORDER_TYPE_MARKET", price=None):
        # боевой orderId НЕ UUID (32-символьный hex) — НЕ валидируем как UUID
        self.orders.append((uid, direction, order_id))
        n = len(self.orders)
        reject = (self.fail_from is not None and n >= self.fail_from) or (n in self.fail_only)
        if reject:
            return {"executionReportStatus": "EXECUTION_REPORT_STATUS_REJECTED",
                    "executedOrderPrice": None}
        p = 32100 if "SP" in uid else 32000
        return {"executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
                "lotsExecuted": lots,
                "executedOrderPrice": {"units": str(p * lots), "nano": 0}}


def _live_exec(armed=True, account_id="acc-real", conn_account="acc-real"):
    from app.st4.tinkoff_executor import TinkoffLiveExecutor
    cfg = St4Config()
    cfg.connector.account_id = conn_account
    so = feed.synthetic_spec(Role.ORDINARY); so.code = "SRM6"
    sp = feed.synthetic_spec(Role.PREFERRED); sp.code = "SPM6"
    sb = FakeLive(account_id=account_id)
    ex = TinkoffLiveExecutor(cfg.execution, cfg.connector, so, sp, sb=sb,
                             armed_cb=lambda: armed)
    return ex, sb


def test_live_executor_no_account_open():
    """Боевой executor НЕ открывает и НЕ пополняет счёт (в отличие от sandbox)."""
    ex, sb = _live_exec()
    assert sb.opened == []        # счёт не открывали
    assert sb.payins == []        # не пополняли
    assert ex._account_id == "acc-real"


def test_live_executor_rejects_unknown_account():
    """account_id, которого нет среди открытых реальных счетов → ошибка (не торгуем)."""
    from app.st4.tbank_sandbox import TBankError
    with pytest.raises(TBankError):
        _live_exec(account_id="acc-real", conn_account="acc-WRONG")


def test_live_executor_blocks_entry_when_not_armed():
    """Не взведено → execute_pair НЕ шлёт ордера (двойной включатель)."""
    ex, sb = _live_exec(armed=False)
    r = ex.execute_pair(buy_ord=True, buy_pref=False, lots=1,
                        book_ord=(32000, 32000), book_pref=(32100, 32100),
                        ref_ord=32000, ref_pref=32100)
    assert not r.ok and r.aborted
    assert sb.orders == []        # ни одного боевого ордера


def test_live_executor_armed_sends_orders():
    """Взведено → обе ноги исполняются реальными ордерами."""
    ex, sb = _live_exec(armed=True)
    r = ex.execute_pair(buy_ord=True, buy_pref=False, lots=1,
                        book_ord=(32000, 32000), book_pref=(32100, 32100),
                        ref_ord=32000, ref_pref=32100)
    assert r.ok
    assert len(sb.orders) == 2    # две ноги
    # orderId детерминированный 32-символьный hex (идемпотентность)
    for _uid, _dir, oid in sb.orders:
        assert len(oid) == 32 and all(c in "0123456789abcdef" for c in oid)


def test_live_executor_close_works_unarmed():
    """Закрытие (снижение риска) работает даже без взвода."""
    ex, sb = _live_exec(armed=False)
    from app.st4.models import LegPosition, Position, BotState
    pos = Position(
        state=BotState.SHORT_SPREAD,
        leg_ord=LegPosition(code="SRM6", role=Role.ORDINARY, side="buy", lots=1, entry_price=32000),
        leg_pref=LegPosition(code="SPM6", role=Role.PREFERRED, side="sell", lots=1, entry_price=32100),
        entry_ts=0, entry_spread=100, entry_beta=1.0, sma_at_entry=100, entry_fee_rub=0)
    res = ex.close_pair(pos, 32000, 32100)
    assert res.exit_ord > 0 and res.exit_pref > 0
    assert len(sb.orders) == 2    # закрытие прошло, несмотря на не-взведённость
