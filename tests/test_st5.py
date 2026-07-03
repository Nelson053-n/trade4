"""Тесты индикаторов ST5 — проверка матчасти на эталонах (look-ahead-safe)."""
from __future__ import annotations

import math

import numpy as np

from app.st5.indicators import (
    KalmanHedge,
    RVRatio,
    ZScore,
    adf_pvalue,
    half_life,
    hurst_rs,
    rolling_ols_beta,
)


def test_kalman_beta_recovers_true_ratio():
    """Kalman β сходится к истинному hedge ratio на синтетике pref = β·ord + шум."""
    rng = np.random.default_rng(7)
    ord_ = np.cumsum(rng.normal(0, 1, 600)) + 100   # случайное блуждание цены
    true_beta = 1.8
    pref = true_beta * ord_ + rng.normal(0, 0.5, 600)
    kf = KalmanHedge(delta=1e-4, obs_noise=1e-3, beta0=1.0)
    betas = []
    for o, p in zip(ord_, pref):
        b, spread, std = kf.step(o, p)
        betas.append(b)
    # после прогрева β близок к истинному
    assert abs(betas[-1] - true_beta) < 0.15, f"β={betas[-1]:.3f} vs {true_beta}"


def test_kalman_innovation_is_spread():
    """Innovation Kalman = spread (pref − β·ord), не вырождается в ноль на стационарной паре."""
    rng = np.random.default_rng(3)
    ord_ = np.cumsum(rng.normal(0, 1, 400)) + 50
    pref = 1.0 * ord_ + rng.normal(0, 1.0, 400)   # спред = шум вокруг 0
    kf = KalmanHedge()
    spreads = [kf.step(o, p)[1] for o, p in zip(ord_, pref)]
    # после прогрева спред колеблется вокруг 0 с ненулевой дисперсией
    tail = np.array(spreads[100:])
    assert abs(tail.mean()) < 1.0
    assert tail.std() > 0.1


def test_rolling_ols_beta_matches_numpy():
    """Rolling OLS β совпадает с прямым numpy polyfit на последнем окне."""
    rng = np.random.default_rng(11)
    x = rng.normal(0, 1, 300)
    y = 2.3 * x + rng.normal(0, 0.1, 300)
    w = 100
    beta = rolling_ols_beta(x, y, w)
    # эталон: polyfit на последнем окне
    ref = np.polyfit(x[-w:], y[-w:], 1)[0]
    assert abs(beta[-1] - ref) < 1e-6
    assert math.isnan(beta[w - 2])   # до полного окна — NaN


def test_zscore_and_dz():
    """Z-score: положителен когда спред выше средней; Δz считается."""
    z = ZScore(ema_span=20, std_window=20)
    out = None
    for v in [0.0] * 25:   # прогрев на константе
        out = z.step(v)
    # резкий выброс вверх → z > 0
    zv, dz = z.step(5.0)
    assert zv is not None and zv > 1.0


def test_adf_pvalue_stationary_vs_random_walk():
    """ADF: низкий p у стационарного (AR1), высокий у случайного блуждания."""
    rng = np.random.default_rng(1)
    # стационарный mean-reverting
    stat = np.zeros(500)
    for i in range(1, 500):
        stat[i] = 0.5 * stat[i - 1] + rng.normal()
    # случайное блуждание (нестационарное)
    rw = np.cumsum(rng.normal(0, 1, 500))
    p_stat = adf_pvalue(stat)
    p_rw = adf_pvalue(rw)
    assert p_stat < 0.05, f"стационарный p={p_stat}"
    assert p_rw > 0.10, f"random walk p={p_rw}"


def test_hurst_mean_reverting_vs_trending():
    """Hurst: <0.5 для mean-reverting (AR1), >0.5 для трендового (random walk)."""
    rng = np.random.default_rng(2)
    mr = np.zeros(1000)
    for i in range(1, 1000):
        mr[i] = -0.5 * mr[i - 1] + rng.normal()   # сильный возврат
    trend = np.cumsum(rng.normal(0, 1, 1000))
    h_mr = hurst_rs(mr)
    h_tr = hurst_rs(trend)
    assert h_mr < 0.5, f"mean-reverting H={h_mr}"
    assert h_tr > 0.5, f"trending H={h_tr}"


def test_half_life_positive_for_mean_reverting():
    """Half-life конечен и положителен для возвратного ряда, inf для random walk."""
    rng = np.random.default_rng(5)
    mr = np.zeros(500)
    for i in range(1, 500):
        mr[i] = 0.7 * mr[i - 1] + rng.normal()   # AR1 с возвратом (λ<0 в Δ-форме)
    hl = half_life(mr)
    assert 0 < hl < 20, f"half_life mean-reverting={hl}"   # AR1(0.7) → HL ≈ 2 бара
    rw = np.cumsum(rng.normal(0, 1, 500))
    hl_rw = half_life(rw)
    # random walk: возврата нет → HL либо inf, либо сильно больше mean-reverting
    assert hl_rw == float("inf") or hl_rw > hl * 5


def test_engine_trades_on_cointegrated_pair():
    """Движок ST5 открывает и закрывает сделки на синтетической коинтегрированной паре."""
    import pandas as pd
    from app.st5.backtest import run_backtest
    from app.st5.config import St5Config
    rng = np.random.default_rng(42)
    n = 2000
    ord_ = np.cumsum(rng.normal(0, 1, n)) + 1000
    spread = np.zeros(n)
    for i in range(1, n):
        spread[i] = 0.97 * spread[i - 1] + rng.normal(0, 3)   # OU mean-reverting
    pref = 1.5 * ord_ + spread
    df = pd.DataFrame({"price_a": ord_, "price_b": pref},
                      index=[i * 600000 for i in range(n)])
    cfg = St5Config()
    cfg.strategy.adf_window = 300
    cfg.strategy.hurst_window = 300
    cfg.strategy.filter_recalc_bars = 20
    cfg.strategy.hurst_max = 0.70   # синтетика даёт высокий R/S Hurst
    m = run_backtest(df, cfg, pair="syn", base_lots=10, fee_per_lot=2.0, half_spread_pts=0.5)
    assert m.trades > 0, "движок не открыл ни одной сделки на mean-reverting паре"
    # причины закрытия осмысленны
    assert set(m.reasons) <= {"exit", "take_partial", "z_stop", "time_stop", "adf_break", "flat_all"}
    # no_entry_windows: дефолт инертен (= как без флага); True отсекает часть входов (вход
    # запрещён на открытии/клиринге/в конце дня). Бэктест 2026-06-29 показал: фильтр режет
    # ПРИБЫЛЬНЫЕ входы на всех 3 реальных парах → в live НЕ включаем, параметр — для исследований.
    m_on = run_backtest(df, cfg, pair="syn", base_lots=10, fee_per_lot=2.0,
                        half_spread_pts=0.5, no_entry_windows=True)
    assert m_on.trades <= m.trades, "фильтр no_entry не должен УВЕЛИЧИВать число входов"


def test_portfolio_limits_gate():
    """Портфельный гейт: лимит на сделку, число позиций, ≤1 на эмитента."""
    from app.st5.service import ST5_PAIRS, St5Portfolio, St5Session
    from app.st5.models import St5Position, St5State
    s = St5Session()
    s.portfolio.capital_rub = 1_000_000.0
    # этот тест проверяет ИМЕННО %-механику → отключаем ₽-лимиты (fallback на % капитала)
    s.cfg.risk.max_go_per_trade_rub = 0.0
    s.cfg.risk.max_go_portfolio_rub = 0.0
    # мок ГО-кэша (иначе pair_go_per_lot лезет в сеть): 1000₽/лот на пару
    St5Portfolio._go_cache = {pid: 1000.0 for pid in ST5_PAIRS}
    # риск (ГО) на сделку 0.5% = 5000: риск 4000 проходит, 6000 — нет
    ok, _ = s.portfolio.can_open("sber", "SBER", 4000.0, s.engines, ST5_PAIRS)
    assert ok
    ok2, reason = s.portfolio.can_open("sber", "SBER", 6000.0, s.engines, ST5_PAIRS)
    assert not ok2 and "сделк" in reason
    # лимит числа позиций: открыты sngr+tatn (2 из 3) → третья (sber) проходит,
    # а если max_open_positions=2 — нет
    for pid, st in (("sngr", St5State.LONG_SPREAD), ("tatn", St5State.SHORT_SPREAD)):
        s.engines[pid].position = St5Position(
            pair=pid, state=st, entry_ts=0, entry_z=-2.5, entry_spread=0.0, entry_beta=1.0,
            lots=10, entry_lots=10, ord_entry=100.0, pref_entry=100.0, half_life=10)
    okN, _ = s.portfolio.can_open("sber", "SBER", 1000.0, s.engines, ST5_PAIRS)
    assert okN   # 2 открыто, лимит 3 → третья проходит
    s.cfg.risk.max_open_positions = 2
    okL, reasonL = s.portfolio.can_open("sber", "SBER", 1000.0, s.engines, ST5_PAIRS)
    assert not okL and "лимит позиций" in reasonL
    s.cfg.risk.max_open_positions = 3
    # кандидат НЕ блокируется собственной позицией (exclude=pair): открыта sber → вход в sber ок
    s.engines["sber"].position = St5Position(
        pair="sber", state=St5State.LONG_SPREAD, entry_ts=0, entry_z=-2.5, entry_spread=0.0,
        entry_beta=1.0, lots=10, entry_lots=10, ord_entry=100.0, pref_entry=100.0, half_life=10)
    okSelf, _ = s.portfolio.can_open("sber", "SBER", 1000.0, s.engines, ST5_PAIRS)
    assert okSelf   # своя позиция не считается «уже есть по эмитенту»
    for pid in ("sber", "sngr", "tatn"):
        s.engines[pid].position = None
    # HALT блокирует всё
    s.portfolio.halt("тест")
    ok4, _ = s.portfolio.can_open("sber", "SBER", 1000.0, s.engines, ST5_PAIRS)
    assert not ok4


def test_go_limit_fixed_rub_not_capital_pct():
    """Лимит ГО на сделку/портфель — ФИКСИРОВАННЫЙ в ₽, НЕ % капитала (для боевого малого счёта).
    Если max_go_*_rub > 0 — применяется он; иначе fallback на %-капитала."""
    from app.st5.service import ST5_PAIRS, St5Portfolio, St5Session
    from app.st5.models import St5Position, St5State
    s = St5Session()
    St5Portfolio._go_cache = {pid: 10_000.0 for pid in ST5_PAIRS}   # ГО 10к/лот на пару
    r = s.cfg.risk
    r.max_go_per_trade_rub = 50_000.0
    r.max_go_portfolio_rub = 300_000.0
    s.portfolio.go_factor = 2.69
    # НЕ зависит от капитала: даже на крошечном счёте лимит остаётся 50к
    s.portfolio.capital_rub = 100_000.0
    # ГО сделки = 10000 × 2.69 = 26900 ≤ 50000 → проходит (при %-логике 0.5%×100к=500 → резало бы)
    ok, _ = s.portfolio.can_open("sber", "SBER", 10_000.0, s.engines, ST5_PAIRS)
    assert ok, "ГО 26900 ≤ потолок 50000 — должно проходить независимо от капитала"
    # ГО сделки = 20000 × 2.69 = 53800 > 50000 → режется по ₽-потолку
    ok2, reason = s.portfolio.can_open("sber", "SBER", 20_000.0, s.engines, ST5_PAIRS)
    assert not ok2 and "сделк" in reason.lower()
    # портфельный ₽-лимит: открыта sngr с ГО 10к×2.69≈26900; новая sber 10к×2.69≈26900;
    # сумма ≈53800 ≤ 300000 → проходит
    s.engines["sngr"].position = St5Position(
        pair="sngr", state=St5State.LONG_SPREAD, entry_ts=0, entry_z=-2.5, entry_spread=0.0,
        entry_beta=1.0, lots=1, entry_lots=1, ord_entry=100.0, pref_entry=100.0, half_life=10)
    okP, _ = s.portfolio.can_open("sber", "SBER", 10_000.0, s.engines, ST5_PAIRS)
    assert okP
    s.engines["sngr"].position = None


def test_go_limits_persist_round_trip(tmp_path):
    """₽-лимиты ГО переживают рестарт (грузятся из session-файла, не сбрасываются в дефолт кода)."""
    from app.st5.service import St5Session
    s = St5Session()
    s._session_file = tmp_path / "s5.json"
    s.cfg.risk.max_go_per_trade_rub = 70_000.0
    s.cfg.risk.max_go_portfolio_rub = 200_000.0
    s.save_session()
    s2 = St5Session()
    s2._session_file = s._session_file
    s2.load_session()
    assert s2.cfg.risk.max_go_per_trade_rub == 70_000.0
    assert s2.cfg.risk.max_go_portfolio_rub == 200_000.0


def test_config_endpoint_sets_go_limits(tmp_path):
    """POST /st5/config принимает ₽-лимиты ГО и применяет к риск-конфигу."""
    from fastapi.testclient import TestClient
    from app.api import app, ST5
    ST5._session_file = tmp_path / "s5.json"
    for e in ST5.engines.values():
        e.position = None                       # flat → смена параметров разрешена
    c = TestClient(app)
    r = c.post("/st5/config", json={"max_go_per_trade_rub": 90_000, "max_go_portfolio_rub": 250_000})
    assert r.status_code == 200, r.text
    assert ST5.cfg.risk.max_go_per_trade_rub == 90_000.0
    assert ST5.cfg.risk.max_go_portfolio_rub == 250_000.0
    # снапшот отдаёт лимиты + go_factor (для калькулятора UI)
    snap = ST5.snapshot()
    assert snap["limits"]["max_go_per_trade_rub"] == 90_000.0
    assert snap["limits"]["max_go_portfolio_rub"] == 250_000.0
    assert "go_factor" in snap["limits"]


def test_real_trading_armed_cooldown():
    """armed_cb: реальная торговля требует взвод + 600с cooldown после старта."""
    import time
    from app.st5.service import St5Session
    s = St5Session()
    # не взведено → False
    assert not s._real_armed()
    # взведено, но только что стартовали → cooldown не прошёл
    s.arm_real(True)
    s.state["session_started"] = time.time()
    assert not s._real_armed()
    # взведено и cooldown прошёл → True
    s.state["session_started"] = time.time() - 700
    assert s._real_armed()
    # рестарт (load_session) снимает взвод
    s.state["real_trading_armed"] = False
    assert not s._real_armed()


def test_order_id_discriminator_no_collision():
    """Идемпотентный order_id: разные операции/seq → разные id (защита частичной фиксации)."""
    import uuid as _uuid
    from app.st5.executor import _disc_order_id
    ids = {
        _disc_order_id("a", "u", 10, "BUY", "entry", 1),
        _disc_order_id("a", "u", 10, "BUY", "take50", 1),
        _disc_order_id("a", "u", 10, "BUY", "take_rest", 1),
        _disc_order_id("a", "u", 10, "BUY", "entry", 2),
    }
    assert len(ids) == 4   # все уникальны
    # sandbox order_id должен быть ВАЛИДНЫМ UUID (требование SandboxService)
    for i in ids:
        _uuid.UUID(i)
    # боевой — sha256-хеш (не UUID)
    assert len(_disc_order_id("a", "u", 10, "BUY", "entry", 1, real=True)) == 32


def test_executor_blocks_real_without_arm():
    """Боевой исполнитель не шлёт ордер, если armed_cb вернул False."""
    from app.st5.executor import St5ExecError, St5PairExecutor
    ex = St5PairExecutor("acc", "SBRF", "SBPR", real=True, armed_cb=lambda: False)
    ex._uid_ord, ex._uid_pref = "uid_o", "uid_p"   # обойти сетевой резолв
    try:
        ex._post("uid_p", 10, "BUY", "entry", 100.0)
        assert False, "должен был отказать"
    except St5ExecError as e:
        assert "не взведена" in str(e)


def test_size_multiplier_tiers():
    """Сайзинг по |z|: тиры 1x/1.5x/2x, запрет при |z|>4."""
    from app.st5.config import St5StrategyConfig
    from app.st5.engine import size_multiplier
    s = St5StrategyConfig()
    assert size_multiplier(2.5, s) == 1.0
    assert size_multiplier(3.0, s) == 1.5
    assert size_multiplier(3.5, s) == 2.0
    assert size_multiplier(4.5, s) is None   # > z_no_entry
    assert size_multiplier(2.0, s) is None   # ниже первого тира


def test_rv_ratio_spikes_on_volatility_burst():
    """RV-ratio > 1 при всплеске краткосрочной волатильности."""
    rng = np.random.default_rng(9)
    calm = list(np.cumsum(rng.normal(0, 0.05, 200)) + 100)
    rv = RVRatio(short=20, long=100)
    out = None
    for v in calm:
        out = rv.step(v)
    # всплеск в последних барах: краткосрочная воля > долгосрочной → ratio > 1
    for v in [100, 130, 80, 140, 70, 150, 60, 160, 90, 130, 75, 145]:
        out = rv.step(float(v))
    assert out is not None and out > 1.0, f"rv_ratio={out}"


# ---------- усыновление позиции со счёта + persist (перенос из st4) ----------

def _session_with_fake_executor(uid_ord="uid_o", uid_pref="uid_p",
                                ord_entry=28000.0, pref_entry=28100.0,
                                entry_ts=1700000000000):
    """St5Session с подменённым исполнителем по паре sber — для теста усыновления оффлайн."""
    from app.st5.service import St5Session
    s = St5Session()
    s.state["sandbox_active"] = True
    s._uid_cache["sber"] = (uid_ord, uid_pref)

    class _FakeEx:
        def entry_prices(self):
            return (ord_entry, pref_entry)   # (ord_entry, pref_entry)
        def broker_entry_ts(self):
            return entry_ts

    s._fake_ex = _FakeEx()
    return s


def test_st5_adopt_long_spread_from_account():
    """Канон st5 (_open): LONG_SPREAD = buy pref + sell ord.
    На счёте обычка −10 (sell) / преф +10 (buy) → LONG_SPREAD. Регресс на инверсию метки."""
    from app.st5.models import St5State
    s = _session_with_fake_executor()
    ex = s._fake_ex
    assert s._adopt_position_from_account("sber", bal_ord=-10, bal_pref=10, executor=ex)
    p = s.engines["sber"].position
    assert p is not None
    assert p.state == St5State.LONG_SPREAD
    assert p.lots == 10 and p.entry_lots == 10
    assert p.ord_entry == 28000.0 and p.pref_entry == 28100.0
    assert p.entry_ts == 1700000000000


def test_st5_adopt_short_spread_sign_matches_direction():
    """Зеркально: обычка +10 (buy) / преф −10 (sell) → SHORT_SPREAD (sell pref + buy ord)."""
    from app.st5.models import St5State
    s = _session_with_fake_executor()
    ex = s._fake_ex
    assert s._adopt_position_from_account("sber", bal_ord=10, bal_pref=-10, executor=ex)
    assert s.engines["sber"].position.state == St5State.SHORT_SPREAD


def test_st5_adopt_rejects_non_paired():
    """Непарная позиция (одна нога / одинаковый знак) → False, не усыновляем."""
    s = _session_with_fake_executor()
    ex = s._fake_ex
    assert not s._adopt_position_from_account("sber", bal_ord=-10, bal_pref=0, executor=ex)
    assert not s._adopt_position_from_account("sber", bal_ord=10, bal_pref=10, executor=ex)
    assert s.engines["sber"].position is None


def test_st5_adopt_entry_ts_fallback_to_last_bar():
    """Брокер не отдал время (sandbox) → entry_ts = last_live_ts[pid], не time.time()."""
    s = _session_with_fake_executor(entry_ts=None)
    s.last_live_ts["sber"] = 1699999000000
    ex = s._fake_ex
    assert s._adopt_position_from_account("sber", bal_ord=-10, bal_pref=10, executor=ex)
    assert s.engines["sber"].position.entry_ts == 1699999000000


def test_st5_position_persist_round_trip(tmp_path):
    """save_session пишет открытые позиции, load_session их восстанавливает (paper round-trip)."""
    from app.st5.service import St5Session
    from app.st5.models import St5Position, St5State
    s = St5Session()
    s._session_file = tmp_path / "session_state_5.json"
    s.engines["sngr"].position = St5Position(
        pair="sngr", state=St5State.SHORT_SPREAD, entry_ts=1700000000000,
        entry_z=2.4, entry_spread=120.0, entry_beta=0.98, lots=2, entry_lots=2,
        ord_entry=24000.0, pref_entry=24120.0, half_life=30.0,
        bars_held=5, partial_done=False, fees_rub=8.0, realized_rub=0.0)
    s.save_session()

    s2 = St5Session()
    s2._session_file = s._session_file
    assert s2.load_session()
    p = s2.engines["sngr"].position
    assert p is not None
    assert p.state == St5State.SHORT_SPREAD
    assert p.lots == 2 and p.entry_z == 2.4 and p.entry_beta == 0.98
    assert p.ord_entry == 24000.0 and p.pref_entry == 24120.0
    assert p.bars_held == 5 and p.fees_rub == 8.0
    # пары без позиции остаются flat
    assert s2.engines["sber"].position is None


# ---------- риск-гейт на РЕАЛЬНОЕ заблокированное ГО (go_factor) ----------

def test_go_factor_default_is_one():
    """Без калибровки go_factor=1.0 — поведение как раньше (оценка ISS)."""
    from app.st5.service import St5Portfolio, St5Session
    s = St5Session()
    assert s.portfolio.go_factor == 1.0
    assert s.portfolio.real_blocked_rub == 0.0


def test_calibrate_go_factor_from_real_blocked():
    """go_factor = real_blocked / сумма ISS-оценок открытых позиций.
    Реально заблокировано 45238 при ISS-оценке открытой tatn 9967 → factor ≈ 4.54."""
    from app.st5.service import ST5_PAIRS, St5Portfolio, St5Session
    from app.st5.models import St5Position, St5State
    s = St5Session()
    St5Portfolio._go_cache = {"tatn": 9967.0, "sber": 11312.0, "sngr": 12096.0}
    s.engines["tatn"].position = St5Position(
        pair="tatn", state=St5State.SHORT_SPREAD, entry_ts=0, entry_z=2.7, entry_spread=0.0,
        entry_beta=1.0, lots=1, entry_lots=1, ord_entry=600.0, pref_entry=560.0, half_life=10)
    s.portfolio.calibrate_go_factor(45238.0, s.engines, ST5_PAIRS)
    assert abs(s.portfolio.go_factor - 45238.0/9967.0) < 0.01
    assert s.portfolio.real_blocked_rub == 45238.0


def test_calibrate_noop_when_flat_or_no_data():
    """Нет открытых позиций или real_blocked=0 → go_factor не трогаем (остаётся прежним)."""
    from app.st5.service import ST5_PAIRS, St5Portfolio, St5Session
    s = St5Session()
    St5Portfolio._go_cache = {pid: 1000.0 for pid in ST5_PAIRS}
    s.portfolio.go_factor = 3.0   # ранее откалибровано
    s.portfolio.calibrate_go_factor(0.0, s.engines, ST5_PAIRS)   # счёт flat
    assert s.portfolio.go_factor == 3.0   # не сбросили в 1.0 на пустых данных


def test_calibrate_ignores_artifact_below_half_iss():
    """Артефакт blocked_margin (≈0) при открытых позициях НЕ должен схлопывать go_factor.
    factor<0.5 физически невозможен (реальное ГО ≥ половины ISS) → калибровка пропускается."""
    from app.st5.service import ST5_PAIRS, St5Portfolio, St5Session
    from app.st5.models import St5Position, St5State
    s = St5Session()
    St5Portfolio._go_cache = {pid: 10_000.0 for pid in ST5_PAIRS}
    s.portfolio.go_factor = 4.18   # ранее нормально откалибровано
    s.engines["tatn"].position = St5Position(
        pair="tatn", state=St5State.SHORT_SPREAD, entry_ts=0, entry_z=2.7, entry_spread=0.0,
        entry_beta=1.0, lots=1, entry_lots=1, ord_entry=600.0, pref_entry=560.0, half_life=10)
    # blocked вернул мусор 58₽ при ISS 10000 → factor 0.0058 < 0.5 → игнор
    s.portfolio.calibrate_go_factor(58.0, s.engines, ST5_PAIRS)
    assert s.portfolio.go_factor == 4.18   # не схлопнулся
    s.engines["tatn"].position = None


def test_load_session_rejects_collapsed_go_factor(tmp_path):
    """load_session с битым go_factor (<0.5, напр. 0.0013 от прошлого артефакта) → дефолт 1.0."""
    import json
    from app.st5.service import St5Session
    s = St5Session()
    f = tmp_path / "s5.json"
    f.write_text(json.dumps({"go_factor": 0.0012886, "capital_rub": 5_000_000}))
    s._session_file = f
    s.load_session()
    assert s.portfolio.go_factor == 1.0

def test_order_book_parses_levels(monkeypatch):
    """order_book парсит bids/asks T-Bank GetOrderBook в {price,qty} + last."""
    from app.st4 import tbank_sandbox as sb
    fake = {
        "bids": [{"price": {"units": "28218", "nano": 0}, "quantity": "4"},
                 {"price": {"units": "28217", "nano": 0}, "quantity": "2"}],
        "asks": [{"price": {"units": "28219", "nano": 0}, "quantity": "1"}],
        "lastPrice": {"units": "28218", "nano": 0},
    }
    monkeypatch.setattr(sb, "_call", lambda *a, **k: fake)
    ob = sb.order_book("uid", depth=10)
    assert ob["bids"][0] == {"price": 28218.0, "qty": 4}
    assert ob["bids"][1]["qty"] == 2
    assert ob["asks"][0] == {"price": 28219.0, "qty": 1}
    assert ob["last"] == 28218.0


def test_live_intent_resumes_after_graceful_restart(tmp_path):
    """resume_live считается по live_intent (намерение оператора), а НЕ по live (факт. состояние).
    graceful restart ставит live=False, но intent остаётся → autoresume стартует."""
    from app.st5.service import St5Session
    s = St5Session()
    s._session_file = tmp_path / "s5.json"
    # оператор запустил торговлю: intent=True
    s.state["live_intent"] = True
    s.state["live"] = True
    # graceful shutdown (lifespan): live→False, но intent НЕ трогаем
    s.state["live"] = False
    s.save_session()
    # рестарт: новый процесс грузит сессию
    s2 = St5Session()
    s2._session_file = s._session_file
    s2.load_session()
    assert s2.state["resume_live"] is True, "intent=True → должен возобновить live после рестарта"


def test_stop_clears_intent_no_resume(tmp_path):
    """Оператор нажал «стоп» (intent=False) → после рестарта НЕ возобновляем (он сам остановил)."""
    from app.st5.service import St5Session
    s = St5Session()
    s._session_file = tmp_path / "s5.json"
    s.state["live_intent"] = False    # оператор остановил
    s.state["live"] = False
    s.save_session()
    s2 = St5Session()
    s2._session_file = s._session_file
    s2.load_session()
    assert s2.state["resume_live"] is False


def test_trade_limit_uses_go_factor():
    """Лимит ГО на сделку считается от ОЦЕНКИ×go_factor. Лимит 0.5% от 1М = 5000.
    risk_rub=2000, factor=4.5 → эффективно 9000 > 5000 → отказ."""
    from app.st5.service import ST5_PAIRS, St5Portfolio, St5Session
    s = St5Session()
    s.portfolio.capital_rub = 1_000_000.0
    s.cfg.risk.max_go_per_trade_rub = 0.0     # проверяем %-fallback → отключаем ₽-лимит
    s.cfg.risk.max_go_portfolio_rub = 0.0
    St5Portfolio._go_cache = {pid: 1000.0 for pid in ST5_PAIRS}
    # factor=1: risk 2000 проходит (< 5000)
    s.portfolio.go_factor = 1.0
    ok, _ = s.portfolio.can_open("sber", "SBER", 2000.0, s.engines, ST5_PAIRS)
    assert ok
    # factor=4.5: тот же risk_rub=2000 → эффективно 9000 > 5000 → отказ
    s.portfolio.go_factor = 4.5
    ok2, reason = s.portfolio.can_open("sber", "SBER", 2000.0, s.engines, ST5_PAIRS)
    assert not ok2 and "сделк" in reason


def test_go_factor_persists_round_trip(tmp_path):
    """go_factor переживает рестарт (был дырой: сбрасывался в 1.0 → первый вход при flat
    гейтился по заниженному ISS-ГО). real_blocked_rub НЕ персистим (текущее заблокированное,
    при flat=0)."""
    from app.st5.service import St5Session
    s = St5Session()
    s._session_file = tmp_path / "session_state_5.json"
    s.portfolio.go_factor = 4.5
    s.portfolio.real_blocked_rub = 45000.0
    s.save_session()
    s2 = St5Session()
    s2._session_file = s._session_file
    s2.load_session()
    assert abs(s2.portfolio.go_factor - 4.5) < 1e-9        # восстановлен
    assert s2.portfolio.real_blocked_rub == 0.0           # НЕ персистится (обновится в refresh_capital)


def test_portfolio_limit_uses_real_blocked():
    """Портфельный лимит считается от РЕАЛЬНО заблокированного (факт), а не суммы ISS-оценок.
    Лимит 5% от 1М = 50000. real_blocked=45000, кандидат (оценка 2000×factor1=2000) →
    45000+2000=47000 < 50000 ок; кандидат побольше → превышение."""
    from app.st5.service import ST5_PAIRS, St5Portfolio, St5Session
    from app.st5.models import St5Position, St5State
    s = St5Session()
    s.portfolio.capital_rub = 1_000_000.0
    s.cfg.risk.max_go_per_trade_rub = 0.0     # проверяем %-fallback → отключаем ₽-лимит
    s.cfg.risk.max_go_portfolio_rub = 0.0
    St5Portfolio._go_cache = {pid: 1000.0 for pid in ST5_PAIRS}
    s.portfolio.go_factor = 1.0
    s.portfolio.real_blocked_rub = 45000.0   # факт по уже открытой tatn
    s.engines["tatn"].position = St5Position(
        pair="tatn", state=St5State.SHORT_SPREAD, entry_ts=0, entry_z=2.7, entry_spread=0.0,
        entry_beta=1.0, lots=1, entry_lots=1, ord_entry=600.0, pref_entry=560.0, half_life=10)
    # кандидат sber, оценка 2000: 45000+2000=47000 < 50000 → ок
    ok, _ = s.portfolio.can_open("sber", "SBER", 2000.0, s.engines, ST5_PAIRS)
    assert ok
    # кандидат с оценкой 6000: 45000+6000=51000 > 50000 → отказ (но сначала пройдёт ли лимит сделки? 6000>5000 — да, отсечётся на сделке)
    # берём 4000 (< 5000 лимит сделки), но 45000+4000=49000 < 50000 → ок; 5500 не проходит лимит сделки.
    # чтобы проверить именно портфельный: уменьшим лимит сделки не будем — поднимем real_blocked
    s.portfolio.real_blocked_rub = 48000.0
    ok2, reason = s.portfolio.can_open("sber", "SBER", 4000.0, s.engines, ST5_PAIRS)
    assert not ok2 and "портфельн" in reason   # 48000+4000=52000 > 50000


def test_reconcile_endpoint_adopts_without_bar(monkeypatch, tmp_path):
    """POST /st5/control/reconcile усыновляет позицию со счёта в движок БЕЗ ожидания бара
    (фикс рассинхрона: на счёте позиция, движок flat → усыновляем сразу)."""
    from fastapi.testclient import TestClient
    from app.api import app, ST5
    from app.st5.service import ST5_PAIRS, St5Portfolio
    ST5._session_file = tmp_path / "s5.json"               # не писать в реальный файл
    monkeypatch.setattr(ST5, "_ensure_uid_cache", lambda pid: True)  # без сети
    ST5.state["sandbox_active"] = True
    St5Portfolio._go_cache = {pid: 1000.0 for pid in ST5_PAIRS}
    for pid in ST5_PAIRS:
        ST5.engines[pid].position = None
    ST5._reconciled = set()
    ST5._uid_cache["tatn"] = ("uo", "up")

    class _FakeEx:
        def broker_lots(self):  # на счёте tatn: обычка +1 / преф −1 → short_spread
            return (1, -1)
        def entry_prices(self):
            return (600.0, 560.0)
        def broker_entry_ts(self):
            return 1700000000000
    monkeypatch.setattr(ST5, "_make_executor", lambda pid: _FakeEx() if pid == "tatn" else None)

    c = TestClient(app)
    r = c.post("/st5/control/reconcile")
    assert r.status_code == 200
    body = r.json()
    tatn = next(x for x in body["pairs"] if x["pair"] == "tatn")
    assert tatn["now"] == "short_spread" and tatn["lots"] == 1
    assert ST5.engines["tatn"].position is not None
    # очистка, чтобы не влиять на другие тесты
    ST5.engines["tatn"].position = None
    ST5.state["sandbox_active"] = False


def test_ensure_uid_cache_fills_from_series(monkeypatch):
    """_ensure_uid_cache резолвит uid по коду СЕРИИ (не asset) и кэширует. Без него reconcile
    спотыкался о пустой кэш (broker_lots по asset-коду промахивается)."""
    from app.st5.service import St5Session
    from app.st4 import data_feed as feed
    from app.st4 import tbank_sandbox as sb
    s = St5Session()
    s._uid_cache.pop("tatn", None)
    s._legs_cache.pop("tatn", None)

    class _Spec:  # объект с .code (код серии)
        def __init__(self, code): self.code = code
    monkeypatch.setattr(feed, "resolve_legs", lambda c4: (_Spec("TTU6"), _Spec("TPU6")))
    monkeypatch.setattr(sb, "find_future", lambda code: {"uid": f"uid-{code}"})
    assert s._ensure_uid_cache("tatn") is True
    assert s._uid_cache["tatn"] == ("uid-TTU6", "uid-TPU6")
    # повторный вызов — из кэша, без резолва
    assert s._ensure_uid_cache("tatn") is True


def test_reconcile_endpoint_fills_uid_cache(monkeypatch, tmp_path):
    """reconcile-эндпоинт усыновляет даже при ПУСТОМ _uid_cache (фикс грабли 2026-06-27):
    _ensure_uid_cache заполняет кэш перед broker_lots."""
    from fastapi.testclient import TestClient
    from app.api import app, ST5
    from app.st5.service import ST5_PAIRS, St5Portfolio
    from app.st4 import data_feed as feed
    from app.st4 import tbank_sandbox as sb
    ST5._session_file = tmp_path / "s5.json"
    ST5.state["sandbox_active"] = True
    St5Portfolio._go_cache = {pid: 1000.0 for pid in ST5_PAIRS}
    for pid in ST5_PAIRS:
        ST5.engines[pid].position = None
    ST5._reconciled = set()
    ST5._uid_cache.clear()   # ПУСТО — как после рестарта
    ST5._legs_cache.clear()

    class _Spec:
        def __init__(self, code): self.code = code
    monkeypatch.setattr(feed, "resolve_legs", lambda c4: (_Spec("TTU6"), _Spec("TPU6")))
    monkeypatch.setattr(sb, "find_future", lambda code: {"uid": f"uid-{code}"})

    class _FakeEx:
        def broker_lots(self): return (1, -1)   # tatn short_spread на счёте
        def entry_prices(self): return (600.0, 560.0)
        def broker_entry_ts(self): return 1700000000000
    # _make_executor вернёт фейк только для tatn (у которой теперь есть uid в кэше)
    orig = ST5._make_executor
    monkeypatch.setattr(ST5, "_make_executor",
                        lambda pid: _FakeEx() if pid == "tatn" and pid in ST5._uid_cache else None)

    c = TestClient(app)
    r = c.post("/st5/control/reconcile")
    assert r.status_code == 200
    tatn = next(x for x in r.json()["pairs"] if x["pair"] == "tatn")
    assert tatn["now"] == "short_spread" and tatn["lots"] == 1
    # очистка
    ST5.engines["tatn"].position = None
    ST5.state["sandbox_active"] = False
    ST5._uid_cache.clear()


def test_adopted_position_survives_warmup_rollback():
    """Усыновлённая позиция имеет bars_held>=1 — откат прогревочных входов (_step_pair снимает
    bars_held==0) её НЕ снесёт. Регресс на баг 2026-06-27 (tatn усыновлялась и сразу сносилась)."""
    s = _session_with_fake_executor()
    ex = s._fake_ex
    assert s._adopt_position_from_account("sber", bal_ord=1, bal_pref=-1, executor=ex)
    p = s.engines["sber"].position
    assert p is not None and p.bars_held >= 1, "усыновлённая позиция должна иметь bars_held>=1"


def test_daily_totals_only_active_days():
    """Итоги /st5/daily (total/missed) считаются ТОЛЬКО по дням с real!=0 — иначе бэктест за
    дни без бота раздувает missed фикцией. Воспроизводим логику итогов (api.py _run)."""
    # бэктест за 5 дней, бот реально торговал только 2 последних
    ideal      = {"d1": 100, "d2": 200, "d3": 300, "d4": 400, "d5": 500}
    with_costs = {"d1":  90, "d2": 180, "d3": 280, "d4": 380, "d5": 470}
    real       = {"d4": 420, "d5": 460}   # real!=0 только d4,d5
    active = {d for d, v in real.items() if v}
    sum_ideal = sum(ideal.get(d, 0) for d in active)
    sum_costs = sum(with_costs.get(d, 0) for d in active)
    sum_real  = sum(real.get(d, 0) for d in active)
    assert len(active) == 2
    assert sum_ideal == 900            # 400+500, НЕ 1500 (не вся история)
    assert sum_costs == 850            # 380+470
    assert sum_real == 880
    missed = sum_costs - sum_real
    assert missed == -30               # реал обогнал бэктест на сопоставимом отрезке
    # антирегресс: старая формула (по всем дням) дала бы фиктивный missed
    old_missed = sum(with_costs.values()) - sum(real.values())
    assert old_missed == 520 and old_missed != missed   # вот та самая «фикция»


def test_adopted_flag_propagates_to_trade():
    """Усыновлённая позиция помечается adopted=True, и флаг доходит до записи закрытой сделки
    (entry-метрики усыновлённой искажены → флаг отличает её в журнале доходности)."""
    from app.st5.models import St5State
    s = _session_with_fake_executor()
    ex = s._fake_ex
    assert s._adopt_position_from_account("sber", bal_ord=1, bal_pref=-1, executor=ex)
    eng = s.engines["sber"]
    assert eng.position.adopted is True
    # закрываем — флаг должен попасть в St5Trade
    tr = eng._close(1700000600000, 0.1, 0.0, 600.0, 560.0, "exit")
    assert tr.adopted is True

def test_normal_position_not_adopted():
    """Обычный вход (engine._open) → adopted=False, сделка тоже не помечена."""
    from app.st5.service import ST5_PAIRS, St5Session
    from app.st5.engine import ST5Engine
    s = St5Session()
    eng = s.engines["sber"]
    # имитируем открытие через _open (z>0 → short)
    eng._open(1700000000000, 2.5, 100.0, 1.0, 100.0, 100.0)
    assert eng.position.adopted is False
    tr = eng._close(1700000600000, 0.1, 0.0, 100.0, 100.0, "exit")
    assert tr.adopted is False


# ============================ Telegram-уведомления (st5) ============================

def test_forts_schedule_session_kinds():
    """forts_kind: сессии/клиринг/выходной по минуте дня и dow."""
    from app.st5.forts_schedule import forts_kind
    # будни (dow=3, среда)
    assert forts_kind(9 * 60 + 30, 3) == "live"      # утренняя
    assert forts_kind(12 * 60, 3) == "live"          # основная
    assert forts_kind(14 * 60 + 2, 3) == "warn"      # дневной клиринг 14:00-14:05
    assert forts_kind(15 * 60, 3) == "live"          # основная после клиринга
    assert forts_kind(18 * 60 + 50, 3) == "warn"     # вечерний клиринг 18:45-19:05
    assert forts_kind(20 * 60, 3) == "live"          # вечерняя
    assert forts_kind(23 * 60 + 55, 3) == "closed"   # после 23:50
    assert forts_kind(3 * 60, 3) == "closed"         # ночь
    # выходные — всегда closed
    assert forts_kind(12 * 60, 6) == "closed"        # суббота
    assert forts_kind(12 * 60, 0) == "closed"        # воскресенье


def test_forts_msk_minute_dow_known_ts():
    """msk_minute_dow для известного UTC-времени: 2026-06-29 06:20 UTC = 09:20 МСК, понедельник."""
    from app.st5.forts_schedule import msk_minute_dow, is_trading_day
    import calendar
    ts = calendar.timegm((2026, 6, 29, 6, 20, 0, 0, 0, 0))  # пн 06:20 UTC
    minute, sec, dow = msk_minute_dow(ts)
    assert minute == 9 * 60 + 20                     # 09:20 МСК
    assert dow == 1                                  # понедельник (JS: вс=0)
    assert is_trading_day(dow) is True


def test_notifier_gated_off_when_disabled_or_no_token(monkeypatch):
    """send() возвращает False без исключений, если выключено / нет токена / нет chat_id."""
    import asyncio
    from app.st5.config import St5NotifyConfig
    from app.st5.notifier import TelegramNotifier
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    # выключено
    n = TelegramNotifier(cfg_cb=lambda: St5NotifyConfig(enabled=False, chat_id="1"))
    assert asyncio.run(n.send("hi")) is False
    # включено, но нет токена
    n2 = TelegramNotifier(cfg_cb=lambda: St5NotifyConfig(enabled=True, chat_id="1"))
    assert asyncio.run(n2.send("hi")) is False
    # включено, токен есть, но нет chat_id
    monkeypatch.setenv("TG_BOT_TOKEN", "xxx")
    n3 = TelegramNotifier(cfg_cb=lambda: St5NotifyConfig(enabled=True, chat_id=""))
    assert asyncio.run(n3.send("hi")) is False


def test_notifier_swallows_network_error(monkeypatch):
    """Сетевая ошибка httpx → send() == False + on_error вызван (торговый цикл не падает)."""
    import asyncio
    from app.st5.config import St5NotifyConfig
    from app.st5 import notifier as tg
    monkeypatch.setenv("TG_BOT_TOKEN", "xxx")
    errs = []

    class _Boom:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): raise RuntimeError("network down")

    monkeypatch.setattr(tg.httpx, "AsyncClient", lambda *a, **k: _Boom())
    n = tg.TelegramNotifier(cfg_cb=lambda: St5NotifyConfig(enabled=True, chat_id="1"),
                            on_error=errs.append)
    assert asyncio.run(n.send("hi")) is False
    assert errs and "не удалась" in errs[0]


def test_notifier_esc_html():
    from app.st5.notifier import esc
    assert esc("a<b>&c") == "a&lt;b&gt;&amp;c"


def test_bot_token_save_load_has(tmp_path, monkeypatch):
    """save/load/has токена бота: файл 0600 + env, has не раскрывает секрет."""
    from app.st5 import notifier as tg
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    monkeypatch.setattr(tg, "_TOKEN_FILE", tmp_path / ".tg_bot_token")
    assert tg.has_bot_token() is False
    tg.save_bot_token("secret123")
    assert tg.has_bot_token() is True
    assert (tmp_path / ".tg_bot_token").read_text() == "secret123"
    assert oct((tmp_path / ".tg_bot_token").stat().st_mode)[-3:] == "600"
    # load из файла в чистый env
    monkeypatch.delenv("TG_BOT_TOKEN", raising=False)
    assert tg.load_bot_token() is True
    import os
    assert os.environ["TG_BOT_TOKEN"] == "secret123"
    tg.save_bot_token("")   # очистка
    assert tg.has_bot_token() is False


def test_notify_config_persists_round_trip(tmp_path):
    """cfg.notify сериализуется в session-файл и восстанавливается (токен — НЕ в файле)."""
    import json
    from app.st5.service import St5Session
    s = St5Session()
    s._session_file = tmp_path / "session_state_5.json"
    s.cfg.notify.enabled = True
    s.cfg.notify.chat_id = "12345"
    s.cfg.notify.before_open_min = 7
    s.save_session()
    raw = json.loads(s._session_file.read_text())
    assert raw["config"]["notify"]["chat_id"] == "12345"
    assert "tg_bot_token" not in json.dumps(raw)   # секрет не утёк в файл
    s2 = St5Session()
    s2._session_file = s._session_file
    s2.load_session()
    assert s2.cfg.notify.enabled is True
    assert s2.cfg.notify.chat_id == "12345"
    assert s2.cfg.notify.before_open_min == 7


def test_schedule_tick_before_open_once(monkeypatch):
    """_schedule_tick шлёт напоминание об открытии один раз в окне до 09:00, не на выходных."""
    import calendar
    from app.st5.service import St5Session
    s = St5Session()
    s.state["data_source"] = "live"
    s.cfg.notify.enabled = True
    s.cfg.notify.before_open_min = 10
    sent = []
    monkeypatch.setattr(s, "_notify", lambda t: sent.append(t))
    # пн 05:52 UTC = 08:52 МСК (в окне 08:50-09:00)
    ts = calendar.timegm((2026, 6, 29, 5, 52, 0, 0, 0, 0))
    s._schedule_tick(ts)
    s._schedule_tick(ts + 60)   # повтор в том же окне/дне — не дублирует
    assert sum(1 for m in sent if "открывается" in m) == 1
    # суббота в том же окне — молчит
    sent.clear()
    s._sched_open_sent = None
    sat = calendar.timegm((2026, 6, 27, 5, 52, 0, 0, 0, 0))
    s._schedule_tick(sat)
    assert not sent


def test_schedule_tick_daily_summary_on_close(monkeypatch):
    """Переход live→closed (конец вечерней сессии) шлёт дневную сводку один раз."""
    import calendar
    from app.st5.service import St5Session
    s = St5Session()
    s.state["data_source"] = "live"
    s.cfg.notify.enabled = True
    sent = []
    monkeypatch.setattr(s, "_notify", lambda t: sent.append(t))
    # пн 20:00 МСК (17:00 UTC) — вечерняя сессия live
    s._schedule_tick(calendar.timegm((2026, 6, 29, 17, 0, 0, 0, 0, 0)))
    assert not [m for m in sent if "Итоги дня" in m]
    # пн 23:55 МСК (20:55 UTC) — закрыто → сводка
    s._schedule_tick(calendar.timegm((2026, 6, 29, 20, 55, 0, 0, 0, 0)))
    assert len([m for m in sent if "Итоги дня" in m]) == 1
    # повторный тик в closed — не дублирует
    s._schedule_tick(calendar.timegm((2026, 6, 29, 20, 56, 0, 0, 0, 0)))
    assert len([m for m in sent if "Итоги дня" in m]) == 1


def test_daily_summary_filters_today(monkeypatch):
    """_daily_summary_text считает P&L только по сегодняшним сделкам (по exit_ts МСК)."""
    import calendar
    from app.st5 import service as svc
    from app.st5.service import St5Session
    s = St5Session()
    # фиксируем «сейчас» = пн 2026-06-29 12:00 МСК (09:00 UTC)
    now = calendar.timegm((2026, 6, 29, 9, 0, 0, 0, 0, 0))
    monkeypatch.setattr(svc.time, "time", lambda: now)
    today_ms = now * 1000
    yest_ms = today_ms - 24 * 3600 * 1000
    s.trades = [
        {"exit_ts": yest_ms, "net_pnl_rub": 1000},        # вчера — игнор
        {"exit_ts": today_ms, "net_pnl_rub": 500},        # сегодня +
        {"exit_ts": today_ms + 3600 * 1000, "net_pnl_rub": -200},  # сегодня −
    ]
    txt = s._daily_summary_text()
    assert "Итоги дня" in txt
    assert "Сделок: 2" in txt          # только 2 сегодняшние
    assert "+300 ₽" in txt             # 500 − 200, вчерашние 1000 не учтены
    assert "win-rate 50%" in txt       # 1 из 2


def test_watchdog_should_restart_predicate():
    """Watchdog перезапускает live-цикл только если он реально завис ПРИ открытой бирже."""
    from app.st5.service import St5Session
    s = St5Session()
    MON_OPEN = 1782723600    # понедельник 12:00 МСК — FORTS открыт (forts_kind=='live')
    SUN_CLOSED = 1782637200  # воскресенье 12:00 МСК — биржа закрыта
    NOW = 100000.0           # произвольный monotonic-момент
    stale = s._watchdog_stale_min * 60

    # не live → никогда не перезапускаем
    s.state["live"] = False
    s._live_hb = NOW - stale - 100
    assert s._watchdog_should_restart(NOW, ts_sec=MON_OPEN) is False

    s.state["live"] = True
    # биржа открыта + проход устарел сильнее порога → перезапуск
    s._live_hb = NOW - stale - 100
    assert s._watchdog_should_restart(NOW, ts_sec=MON_OPEN) is True
    # тот же застой, но биржа ЗАКРЫТА (ночь/выходной) → НЕ перезапуск (баров нет легитимно)
    assert s._watchdog_should_restart(NOW, ts_sec=SUN_CLOSED) is False
    # биржа открыта, но проход свежий → не трогаем
    s._live_hb = NOW - 60
    assert s._watchdog_should_restart(NOW, ts_sec=MON_OPEN) is False
    # цикл ещё ни разу не завершил проход (_live_hb==0) → не считаем зависанием
    s._live_hb = 0.0
    assert s._watchdog_should_restart(NOW, ts_sec=MON_OPEN) is False


def test_day_pnl_resets_on_new_day():
    """day_pnl обнуляется при смене торгового дня (МСК), а не копит за всё время."""
    import calendar
    from app.st5.service import St5Portfolio
    from app.st5.config import St5Config
    p = St5Portfolio(St5Config())
    d1 = calendar.timegm((2026, 6, 29, 9, 0, 0, 0, 0, 0)) * 1000   # пн 12:00 МСК
    d1b = d1 + 3600 * 1000                                          # пн 13:00 МСК
    d2 = calendar.timegm((2026, 6, 30, 9, 0, 0, 0, 0, 0)) * 1000   # вт 12:00 МСК
    p.on_trade(500, d1)
    p.on_trade(-200, d1b)
    assert p.day_pnl_rub == 300                # день 1: 500 − 200
    p.on_trade(1000, d2)
    assert p.day_pnl_rub == 1000               # новый день → обнулилось, не 1300


def test_day_pnl_recomputed_from_journal_on_load(monkeypatch):
    """load_session пересчитывает day_pnl из СЕГОДНЯШНИХ сделок, игнорируя копивший счётчик файла."""
    import calendar, json
    from app.st5 import service as svc
    from app.st5.service import St5Session
    now = calendar.timegm((2026, 6, 29, 9, 0, 0, 0, 0, 0))   # пн 12:00 МСК
    monkeypatch.setattr(svc.time, "time", lambda: now)
    today_ms = now * 1000
    yest_ms = today_ms - 24 * 3600 * 1000
    s = St5Session()
    payload = {
        "day_pnl_rub": 9999,    # «накопленный за всё время» из старой версии — должен игнорироваться
        "trades": [
            {"exit_ts": yest_ms, "net_pnl_rub": 1000},   # вчера — не в дневном
            {"exit_ts": today_ms, "net_pnl_rub": 500},   # сегодня
            {"exit_ts": today_ms + 3600 * 1000, "net_pnl_rub": -200},  # сегодня
        ],
    }
    monkeypatch.setattr(type(s._session_file), "read_text", lambda self: json.dumps(payload))
    monkeypatch.setattr(type(s._session_file), "exists", lambda self: True)
    s.load_session()
    assert s.portfolio.day_pnl_rub == 300      # 500 − 200, не 9999 и не +1000 вчерашних


def test_pair_z_params_calibrated_honest_metric():
    """z-параметры пар — калибровка 2026-07-02 на ЧЕСТНОЙ метрике (P&L по фактическим β-ногам
    после фикса α-смещения; 4 сегмента дек-2025–июль-2026, робастность к издержкам 0.5–2пт).
    Меняли — пересними бэктест ЧЕСТНЫМ движком (старые α-завышенные цифры не сравнимы)."""
    from app.st5.service import ST5_PAIRS, St5Session
    want = {"sber": (1.25, 0.25, 0.0), "sngr": (1.5, 0.5, 1.0), "tatn": (1.75, 0.25, 1.0)}
    s = St5Session()
    for pid, (ze, zx, zp) in want.items():
        ov = ST5_PAIRS[pid][4]
        assert (ov["z_entry"], ov["z_exit_full"], ov["z_take_partial"]) == (ze, zx, zp), pid
        # _pair_cfg должен реально применить оверрайд к движку
        st = s.engines[pid].cfg.strategy
        assert (st.z_entry, st.z_exit_full, st.z_take_partial) == (ze, zx, zp), pid


# ============================ runtime per-pair оверрайды + хранилище стратегий ============================

def test_pair_overrides_take_priority_in_pair_cfg():
    """pair_overrides[pid] перекрывают ST5_PAIRS-оверрайды из кода в _pair_cfg."""
    from app.st5.service import St5Session
    s = St5Session()
    assert s.engines["sber"].cfg.strategy.z_exit_full == 0.25    # из кода
    s.pair_overrides["sber"] = {"z_exit_full": 0.1}
    cfg = s._pair_cfg("sber")
    assert cfg.strategy.z_exit_full == 0.1                       # runtime перекрыл


def test_apply_overrides_updates_live_engine_when_flat():
    """apply_overrides пересобирает движок пары на лету (позиция flat)."""
    from app.st5.service import St5Session
    s = St5Session()
    assert s.engines["sngr"].position is None
    ok, _ = s.apply_overrides({"sngr": {"z_exit_full": 0.1, "z_entry": 1.5}})
    assert ok is True
    assert s.engines["sngr"].cfg.strategy.z_exit_full == 0.1
    assert s.engines["sngr"].cfg.strategy.z_entry == 1.5
    assert s.pair_overrides["sngr"]["z_entry"] == 1.5


def test_apply_overrides_blocked_with_open_position():
    """apply_overrides НЕ трогает пару с открытой позицией (рассинхрон движок↔счёт)."""
    from app.st5.service import St5Session
    from app.st5.engine import ST5Engine
    s = St5Session()
    s.engines["tatn"]._open(1700000000000, 2.5, 100.0, 0.1, 100.0, 100.0)
    assert s.engines["tatn"].position is not None
    before = s.engines["tatn"].cfg.strategy.z_exit_full
    ok, reason = s.apply_overrides({"tatn": {"z_exit_full": 0.1}})
    assert ok is False
    assert "позиц" in reason.lower()
    assert s.engines["tatn"].cfg.strategy.z_exit_full == before   # не изменилось


def test_pair_overrides_survive_session_round_trip(tmp_path, monkeypatch):
    """pair_overrides переживают save/load session."""
    from app.st5 import service as svc
    from app.st5.service import St5Session
    s = St5Session()
    s._session_file = tmp_path / "ss5.json"
    s.pair_overrides["sber"] = {"z_exit_full": 0.25}
    s.save_session()
    s2 = St5Session()
    s2._session_file = tmp_path / "ss5.json"
    assert s2.load_session() is True
    assert s2.pair_overrides["sber"] == {"z_exit_full": 0.25}
    assert s2.engines["sber"].cfg.strategy.z_exit_full == 0.25    # применилось к движку


def test_capture_current_snapshots_effective_params():
    """capture_current снимает ДЕЙСТВУЮЩИЕ per-pair параметры (код + runtime-оверрайды)."""
    from app.st5.service import St5Session
    s = St5Session()
    s.pair_overrides["sngr"] = {"z_exit_full": 0.1}
    snap = s.capture_current()
    assert snap["sber"]["z_exit_full"] == 0.25     # из кода
    assert snap["sngr"]["z_exit_full"] == 0.1      # runtime перекрыл
    assert "z_entry" in snap["sber"]               # снимаются все ключевые параметры


def test_strategy_store_save_list_load(tmp_path, monkeypatch):
    """strategy_store: save → list → load round-trip с метриками бэктеста."""
    from app.st5 import strategy_store as store
    monkeypatch.setattr(store, "_STORE_DIR", tmp_path)
    params = {"sber": {"z_exit_full": 0.5, "z_entry": 1.25}}
    backtest = {"sber": {"net": 8801, "win": 99, "sharpe": 15.8}}
    sid = store.save_strategy(name="test-v1", params=params, backtest=backtest,
                              window="90д ISS", note="проба", source="manual", ts_ms=1782735000000)
    lst = store.list_strategies()
    assert any(x["id"] == sid for x in lst)
    rec = store.load_strategy(sid)
    assert rec["name"] == "test-v1"
    assert rec["params"]["sber"]["z_exit_full"] == 0.5
    assert rec["backtest"]["sber"]["net"] == 8801
    assert rec["window"] == "90д ISS"


def test_margin_timeline_reconstructs_go_and_positions(tmp_path, monkeypatch):
    """GET /st5/margin_timeline: реконструирует залог по 10-минуткам из журнала сделок и
    считает число одновременно открытых позиций (перекрытие интервалов)."""
    import datetime as _dt
    from fastapi.testclient import TestClient
    from app.api import app, ST5
    from app.st5.service import St5Portfolio
    ST5._session_file = tmp_path / "s5.json"
    for e in ST5.engines.values():
        e.position = None                       # без «текущих» позиций — только журнал
    # фиксированное ГО ног пары (не дёргать ISS в тесте): 5к+5к = 10к за пару 1+1
    monkeypatch.setattr(St5Portfolio, "pair_leg_margins",
                        classmethod(lambda cls, pid: (5_000.0, 5_000.0)))
    ST5.portfolio.go_factor = 2.0
    # день: две пары, tatn 10:00-10:30, sber 10:10-10:20 (перекрытие в слоте 10:10-10:20)
    MSK = _dt.timezone(_dt.timedelta(hours=3))
    base = int(_dt.datetime(2026, 7, 2, 10, 0, tzinfo=MSK).timestamp() * 1000)
    ST5.trades = [
        {"pair": "tatn", "entry_ts": base, "exit_ts": base + 30 * 60_000, "lots": 1},
        {"pair": "sber", "entry_ts": base + 10 * 60_000, "exit_ts": base + 20 * 60_000, "lots": 1},
    ]
    c = TestClient(app)
    r = c.get("/st5/margin_timeline?date=2026-07-02")
    assert r.status_code == 200, r.text
    d = r.json()
    rows = {row["time"]: row for row in d["rows"]}
    # 10:00 — только tatn: 1 поз, ГО = 10000×1×2.0 = 20000
    assert rows["10:00"]["positions"] == 1 and rows["10:00"]["go_rub"] == 20_000
    # 10:10 — tatn+sber: 2 поз, ГО = 40000 (пик)
    assert rows["10:10"]["positions"] == 2 and rows["10:10"]["go_rub"] == 40_000
    # 10:20 — снова только tatn (sber вышел)
    assert rows["10:20"]["positions"] == 1
    assert d["peak_positions"] == 2 and d["peak_rub"] == 40_000


# ============================ этап 2: β-сайзинг ног + P&L по фактическим ногам ============================

def test_hedge_unit_beta_ratios():
    """β-юнит ног: unit_ord/unit_pref ≈ β. sber β≈1 → (1,1); sngr β≈2.5 → (5,2);
    tatn β≈0.095 → (1,10). Вырожденный β → защитный (1,1)."""
    from app.st5.engine import hedge_unit
    assert hedge_unit(0.997) == (1, 1)
    assert hedge_unit(2.525) == (5, 2)
    assert hedge_unit(0.095) == (1, 10)
    assert hedge_unit(0.0) == (1, 1)


def test_legs_pnl_not_beta_model():
    """P&L сделки = экономика ФАКТИЧЕСКИХ ног (цены исполнения × лоты ног), а не β-модель
    спреда. long_spread: buy pref + sell ord → P&L = Δpref·pref_lots − Δord·ord_lots."""
    from app.st5.config import St5Config
    from app.st5.engine import ST5Engine
    from app.st5.models import St5Position, St5State
    eng = ST5Engine("t", St5Config(), base_lots=1, fee_per_lot=2.0)
    eng.position = St5Position(
        pair="t", state=St5State.LONG_SPREAD, entry_ts=1, entry_z=-2.0, entry_spread=0.0,
        entry_beta=0.1, lots=10, entry_lots=10, ord_entry=100.0, pref_entry=50.0,
        half_life=float("inf"), ord_lots=1, units=1, unit_ord=1, unit_pref=10)
    tr = eng._close(2, 0.0, 0.0, 101.0, 51.0, "exit")
    # ноги: преф +1·10 лотов = +10; обычка (шорт) −1·1 лот = −1 → gross 9
    assert tr.gross_pnl_rub == 9.0
    # комиссия round-trip обеих ног: 2·(1+10)·2₽ = 44
    assert tr.fees_rub == 44.0
    assert tr.net_pnl_rub == 9.0 - 44.0
    assert tr.ord_lots == 1 and tr.lots == 10


def test_take_partial_closes_whole_units():
    """Частичная фиксация закрывает ЦЕЛЫЕ юниты (обе ноги пропорционально β), не половину префа."""
    from app.st5.config import St5Config
    from app.st5.engine import ST5Engine
    from app.st5.models import St5Position, St5State
    eng = ST5Engine("t", St5Config(), base_lots=1, fee_per_lot=2.0)
    eng.position = St5Position(
        pair="t", state=St5State.SHORT_SPREAD, entry_ts=1, entry_z=2.0, entry_spread=0.0,
        entry_beta=2.5, lots=4, entry_lots=4, ord_entry=100.0, pref_entry=250.0,
        half_life=float("inf"), ord_lots=10, units=2, unit_ord=5, unit_pref=2)
    tr = eng._take_partial(2, 0.5, 0.0, 100.0, 250.0)
    assert tr.lots == 2 and tr.ord_lots == 5          # закрыт 1 юнит: 2 префа + 5 обычек
    p = eng.position
    assert p.units == 1 and p.lots == 2 and p.ord_lots == 5   # остался 1 юнит
    assert p.partial_done is True


def test_open_uses_beta_units_and_max_units_cap():
    """_open сайзит ноги β-юнитом от текущего β и капит юниты max_units (ликвидность ноги)."""
    from app.st5.config import St5Config
    from app.st5.engine import ST5Engine
    c = St5Config()
    c.strategy.max_units = 1
    eng = ST5Engine("t", c, base_lots=3)              # base 3 юнита, кап → 1
    eng.filt.half_life = 20.0
    eng._open(1, -2.5, 0.0, 0.095, 49000.0, 4700.0)   # β≈0.095 → юнит (1 обычка, 10 префов)
    p = eng.position
    assert p.units == 1
    assert p.unit_ord == 1 and p.unit_pref == 10
    assert p.lots == 10 and p.ord_lots == 1


def test_adopt_unequal_legs_from_account():
    """Усыновление β-ног: обычка +1 / преф −10 → SHORT_SPREAD, ноги фактические, юнит неделим."""
    from app.st5.models import St5State
    s = _session_with_fake_executor()
    assert s._adopt_position_from_account("sber", bal_ord=1, bal_pref=-10, executor=s._fake_ex)
    p = s.engines["sber"].position
    assert p.state == St5State.SHORT_SPREAD
    assert p.lots == 10 and p.ord_lots == 1
    assert p.units == 1                                # частичной фиксации не будет

def test_position_matches_lots_beta_legs():
    """Сверка со счётом сравнивает ФАКТИЧЕСКИЕ ноги (ord_lots ≠ lots), а не равные."""
    from app.st5.models import St5Position, St5State
    s = _session_with_fake_executor()
    eng = s.engines["sber"]
    eng.position = St5Position(
        pair="sber", state=St5State.LONG_SPREAD, entry_ts=1, entry_z=-2.0, entry_spread=0.0,
        entry_beta=0.1, lots=10, entry_lots=10, ord_entry=100.0, pref_entry=50.0,
        half_life=float("inf"), ord_lots=1, units=1, unit_ord=1, unit_pref=10)
    assert s._position_matches_lots(eng, bal_ord=-1, bal_pref=10) is True
    assert s._position_matches_lots(eng, bal_ord=-10, bal_pref=10) is False


def test_position_from_json_legacy_equal_legs():
    """Legacy session-файл без полей β-ног → равные ноги (прежнее поведение исполнителя)."""
    from app.st5.service import St5Session
    from app.st5.models import St5State
    d = {"pair": "sber", "state": "long_spread", "entry_ts": 1, "entry_z": -2.0,
         "entry_spread": 0.0, "entry_beta": 1.0, "lots": 3, "entry_lots": 3,
         "ord_entry": 100.0, "pref_entry": 101.0, "half_life": 20.0}
    p = St5Session._position_from_json(d)
    assert p.state == St5State.LONG_SPREAD
    assert p.ord_lots == 3 and p.units == 3 and p.unit_ord == 1 and p.unit_pref == 1


def test_executor_posts_different_leg_lots():
    """open_pair/close_pair шлют РАЗНЫЕ лоты ног (β-сайзинг): преф первым, лоты не перепутаны."""
    from app.st5.executor import St5PairExecutor
    ex = St5PairExecutor("acc", "TATN", "TATP", real=False)
    ex._uid_ord, ex._uid_pref = "uid_o", "uid_p"
    calls = []
    ex._post = lambda uid, lots, direction, op, ref: calls.append((uid, lots, direction, op)) or {}
    ex.open_pair(True, 1, 10, 49000.0, 4700.0)         # long: buy pref ×10, sell ord ×1
    assert calls[0] == ("uid_p", 10, "BUY", "entry")   # преф первым
    assert calls[1] == ("uid_o", 1, "SELL", "entry")
    calls.clear()
    ex.close_pair(True, 1, 10, 49000.0, 4700.0)
    assert calls[0] == ("uid_p", 10, "SELL", "flat")
    assert calls[1] == ("uid_o", 1, "BUY", "flat")


def test_pos_risk_by_actual_legs():
    """Риск позиции (ГО) = Σ leg_margin × ФАКТИЧЕСКИЕ лоты ноги (β-ноги, не lots×2)."""
    from app.st5.config import St5Config
    from app.st5.engine import ST5Engine
    from app.st5.models import St5Position, St5State
    from app.st5.service import ST5_PAIRS, St5Portfolio
    St5Portfolio._go_cache = {"tatn": (6000.0, 600.0)}   # (обычка, преф) ₽/лот
    eng = ST5Engine("tatn", St5Config(), base_lots=1)
    eng.position = St5Position(
        pair="tatn", state=St5State.LONG_SPREAD, entry_ts=1, entry_z=-2.0, entry_spread=0.0,
        entry_beta=0.1, lots=10, entry_lots=10, ord_entry=49000.0, pref_entry=4700.0,
        half_life=float("inf"), ord_lots=1, units=1, unit_ord=1, unit_pref=10)
    risk = St5Portfolio._pos_risk("tatn", eng)
    assert risk == 6000.0 * 1 + 600.0 * 10               # 12000, а не (6000+600)×10
    St5Portfolio._go_cache = {}


# ============================ этап 3: сверка executed_lots + периодический reconcile ============================

def _ex_with_fills(fills):
    """Исполнитель с моком _post: fills — очередь ответов {'lotsExecuted': N} либо Exception.
    Возвращает (executor, calls) — calls накапливает (uid, lots, direction, op)."""
    from app.st5.executor import St5PairExecutor
    ex = St5PairExecutor("acc", "SBRF", "SBPR", real=False)
    ex._uid_ord, ex._uid_pref = "uid_o", "uid_p"
    calls = []
    queue = list(fills)

    def _post(uid, lots, direction, op, ref):
        calls.append((uid, lots, direction, op))
        r = queue.pop(0) if queue else {}
        if isinstance(r, Exception):
            raise r
        return r
    ex._post = _post
    return ex, calls


def test_partial_fill_first_leg_unwinds_executed():
    """Частичный филл ПЕРВОЙ ноги (преф): откатываем РЕАЛЬНО налитое (7, не 10), вход отменён."""
    from app.st5.executor import St5ExecError
    ex, calls = _ex_with_fills([{"lotsExecuted": 7}, {}])
    try:
        ex.open_pair(True, 1, 10, 49000.0, 4700.0)
        assert False, "должен был поднять St5ExecError"
    except St5ExecError as e:
        assert "7/10" in str(e)
    assert calls[0] == ("uid_p", 10, "BUY", "entry")
    assert calls[1] == ("uid_p", 7, "SELL", "unwind")   # откат именно 7 налитых
    assert len(calls) == 2                              # до обычки не дошли


def test_partial_fill_second_leg_unwinds_both():
    """Частичный филл ВТОРОЙ ноги (обычка): откатываем налитую обычку И весь преф."""
    from app.st5.executor import St5ExecError
    ex, calls = _ex_with_fills([{"lotsExecuted": 10}, {"lotsExecuted": 2}, {}, {}])
    try:
        ex.open_pair(True, 5, 10, 49000.0, 4700.0)
        assert False
    except St5ExecError as e:
        assert "2/5" in str(e)
    ops = [(c[0], c[1], c[3]) for c in calls]
    assert ("uid_o", 2, "unwind") in ops                # откат 2 налитых обычек
    assert ("uid_p", 10, "unwind") in ops               # откат всего префа


def test_full_fill_and_missing_field_ok():
    """Полный филл и ответ БЕЗ поля executed (совместимость) → вход проходит без отката."""
    ex, calls = _ex_with_fills([{"lotsExecuted": 10}, {}])   # второй ответ без поля
    assert ex.open_pair(True, 1, 10, 49000.0, 4700.0) == {"ok": True}
    assert len(calls) == 2 and all(c[3] == "entry" for c in calls)


def test_close_pair_underfill_raises_no_unwind():
    """Недолив ЗАКРЫВАЮЩЕГО ордера: ошибку поднимаем (пара захалтится), но обратных ордеров
    НЕ шлём (закрытие = снижение риска, откат поднял бы риск обратно)."""
    from app.st5.executor import St5ExecError
    ex, calls = _ex_with_fills([{"lotsExecuted": 6}, {"lotsExecuted": 1}])
    try:
        ex.close_pair(True, 1, 10, 49000.0, 4700.0)
        assert False
    except St5ExecError as e:
        assert "6/10" in str(e)
    assert len(calls) == 2                              # оба закрывающих, никаких unwind
    assert all(c[3] == "flat" for c in calls)


def test_periodic_reconcile_logs_mismatch_once(monkeypatch):
    """Периодическая сверка: расхождение ног логируется ОДИН раз на сигнатуру (анти-спам),
    счёт не трогаем, позицию движка не сносим."""
    import asyncio
    from app.st5.models import St5Position, St5State
    s = _session_with_fake_executor()
    s._reconciled.add("sber")
    s._periodic_reconcile_every_s = 0                    # без ожидания в тесте
    eng = s.engines["sber"]
    eng.position = St5Position(
        pair="sber", state=St5State.LONG_SPREAD, entry_ts=1, entry_z=-2.0, entry_spread=0.0,
        entry_beta=1.0, lots=1, entry_lots=1, ord_entry=100.0, pref_entry=101.0,
        half_life=float("inf"), ord_lots=1, units=1)
    s._fake_ex.broker_lots = lambda: (0, 0)              # счёт пуст, движок держит позицию
    monkeypatch.setattr(s, "_make_executor", lambda pid: s._fake_ex)
    monkeypatch.setattr(s, "_ensure_uid_cache", lambda pid: pid == "sber")
    asyncio.run(s._periodic_reconcile())
    warns = [e for e in s.events if "ноги разошлись" in e["message"]]
    assert len(warns) == 1
    assert eng.position is not None                      # позицию не тронули
    s._last_periodic_reconcile = 0.0
    asyncio.run(s._periodic_reconcile())                 # та же сигнатура → без дубля
    warns = [e for e in s.events if "ноги разошлись" in e["message"]]
    assert len(warns) == 1


def test_periodic_reconcile_flat_foreign_legs_info(monkeypatch):
    """ОБЩИЙ счёт (dedicated_account=False): чужие ноги при flat → info-лог, без действий."""
    import asyncio
    s = _session_with_fake_executor()
    s.cfg.dedicated_account = False
    s._reconciled.add("sber")
    s._periodic_reconcile_every_s = 0
    s.engines["sber"].position = None
    s._fake_ex.broker_lots = lambda: (3, -3)
    monkeypatch.setattr(s, "_make_executor", lambda pid: s._fake_ex)
    monkeypatch.setattr(s, "_ensure_uid_cache", lambda pid: pid == "sber")
    asyncio.run(s._periodic_reconcile())
    infos = [e for e in s.events if "движок flat" in e["message"]]
    assert len(infos) == 1 and infos[0]["kind"] == "info"


def test_periodic_reconcile_dedicated_closes_orphan_legs(monkeypatch):
    """ВЫДЕЛЕННЫЙ счёт (дефолт): голая нога при flat-движке → автозакрытие маркетом + warn.
    Регресс на инцидент 02.07 18:20 (сорванный unwind оставил SGU6 −6 на 5 часов)."""
    import asyncio
    s = _session_with_fake_executor()
    assert s.cfg.dedicated_account is True            # дефолт после развода счетов
    s._reconciled.add("sber")
    s._periodic_reconcile_every_s = 0
    s.engines["sber"].position = None
    orders = []
    s._fake_ex.broker_lots = lambda: (0, -6)          # голый шорт 6 префов
    s._fake_ex._uids = lambda: ("uid_o", "uid_p")
    s._fake_ex._post = lambda uid, lots, d, op, ref: orders.append((uid, lots, d, op))
    monkeypatch.setattr(s, "_make_executor", lambda pid: s._fake_ex)
    monkeypatch.setattr(s, "_ensure_uid_cache", lambda pid: pid == "sber")
    asyncio.run(s._periodic_reconcile())
    assert ("uid_p", 6, "BUY", "cleanup") in orders   # закрыли обратной стороной
    assert any("ГОЛАЯ НОГА" in e["message"] and e["kind"] == "warn" for e in s.events)
    assert any("голая нога закрыта" in e["message"] for e in s.events)


# ============================ per-аккаунт токены (st4/st5 на разных токенах) ============================

def test_account_token_roundtrip(tmp_path, monkeypatch):
    """set_account_token/_account_token: привязка переживает перезагрузку кэша, снятие работает."""
    from app.st4 import tbank_sandbox as sb
    monkeypatch.setattr(sb, "_ACCOUNT_TOKENS_FILE", tmp_path / "acct.json")
    monkeypatch.setattr(sb, "_account_tokens", None)
    sb.set_account_token("acc-5", "t.NEW")
    assert sb._account_token("acc-5") == "t.NEW"
    assert sb._account_token("acc-4") is None        # чужой счёт — общий токен
    monkeypatch.setattr(sb, "_account_tokens", None)  # сброс кэша → перечитать файл
    assert sb._account_token("acc-5") == "t.NEW"
    sb.set_account_token("acc-5", "")                 # снятие привязки
    assert sb._account_token("acc-5") is None


def test_account_scoped_calls_use_account_token(tmp_path, monkeypatch):
    """Вызовы со счётом (portfolio/positions/post_order/pay_in) идут с токеном СЧЁТА,
    остальные — с общим. Гарантия разделения st4 (общий токен) и st5 (свой)."""
    from app.st4 import tbank_sandbox as sb
    monkeypatch.setattr(sb, "_ACCOUNT_TOKENS_FILE", tmp_path / "acct.json")
    monkeypatch.setattr(sb, "_account_tokens", None)
    sb.set_account_token("acc-st5", "t.ST5")
    used = []

    def fake_call(service, method, body, _retries=3, token=None):
        used.append((method, token))
        return {"accounts": [], "balance": None}
    monkeypatch.setattr(sb, "_call", fake_call)
    sb.portfolio("acc-st5")
    sb.positions("acc-st5")
    sb.post_order("acc-st5", "uid", 1, "ORDER_DIRECTION_BUY", "oid")
    sb.portfolio("acc-st4")                           # счёт БЕЗ привязки
    sb.list_accounts()
    assert used[0] == ("GetSandboxPortfolio", "t.ST5")
    assert used[1] == ("GetSandboxPositions", "t.ST5")
    assert used[2] == ("PostSandboxOrder", "t.ST5")
    assert used[3] == ("GetSandboxPortfolio", None)   # st4-счёт → общий токен
    assert used[4] == ("GetSandboxAccounts", None)


def test_st5_connector_sets_account_token(monkeypatch, tmp_path):
    """/st5/connector с account_token закрепляет токен ЗА СЧЁТОМ st5, не меняя общий."""
    from fastapi.testclient import TestClient
    from app.api import app, ST5
    from app.st4 import tbank_sandbox as sb
    monkeypatch.setattr(sb, "_ACCOUNT_TOKENS_FILE", tmp_path / "acct.json")
    monkeypatch.setattr(sb, "_account_tokens", None)
    saved_global = []
    monkeypatch.setattr(sb, "save_token", lambda t: saved_global.append(t))
    for e in ST5.engines.values():
        e.position = None
    c = TestClient(app)
    r = c.post("/st5/connector", json={"mode": "tbank_sandbox",
                                       "account_id": "acc-new",
                                       "account_token": "t.ST5ONLY"})
    assert r.status_code == 200, r.text
    assert sb._account_token("acc-new") == "t.ST5ONLY"
    assert saved_global == []                          # общий токен НЕ трогали


def test_quantity_lots_survives_session_round_trip(tmp_path):
    """quantity_lots (базовые юниты на вход) переживает save/load session — рестарт
    не должен сбрасывать заданный оператором объём в дефолт кода (регресс 02.07)."""
    from app.st5.service import St5Session
    s = St5Session()
    s._session_file = tmp_path / "s5.json"
    s.cfg.execution.quantity_lots = 2
    for eng in s.engines.values():
        eng.base_lots = 2
    s.save_session()
    s2 = St5Session()
    s2._session_file = tmp_path / "s5.json"
    assert s2.load_session() is True
    assert s2.cfg.execution.quantity_lots == 2
    assert all(e.base_lots == 2 for e in s2.engines.values())


# ============================ журнал упущенных входов ============================

def test_entry_block_reason_explains_filters():
    """entry_block_reason: |z|>порога, но фильтры не пускают — возвращает (что сработало, почему)."""
    from app.st5.config import St5Config
    from app.st5.engine import ST5Engine
    eng = ST5Engine("t", St5Config(), base_lots=1)
    eng.cfg.strategy.z_entry = 1.25
    eng.cfg.strategy.size_tiers = [(1.25, 4.0, 1.0)]   # тиры покрывают z (иначе свой блок)
    eng.last_z = 2.0
    eng.filt.cointegrated = False
    eng.filt.mean_reverting = True
    eng.filt.calm_regime = True
    eng.filt.adf_p = 0.3
    blk = eng.entry_block_reason()
    assert blk is not None
    fired, reason = blk
    assert "2.00" in fired and "SHORT" in fired
    assert "коинтеграции" in reason and "0.300" in reason
    # сигнала нет → None
    eng.last_z = 0.5
    assert eng.entry_block_reason() is None
    # все фильтры зелёные и z в тирах → None (вход бы состоялся, пропуска нет)
    eng.last_z = 2.0
    eng.filt.cointegrated = True
    assert eng.entry_block_reason() is None


def test_log_missed_antispam_and_snapshot():
    """log_missed: один пропуск на (пара, бар); попадает в snapshot и session."""
    from app.st5.service import St5Session
    s = St5Session()
    s.log_missed("sber", 1700000000000, "|z|=2.0", "портфельный гейт: лимит ГО")
    s.log_missed("sber", 1700000000000, "|z|=2.0", "дубль того же бара")
    s.log_missed("sber", 1700000600000, "|z|=2.1", "следующий бар — пишется")
    assert len(s.missed) == 2
    snap = s.snapshot()
    assert len(snap["missed"]) == 2
    assert snap["missed"][0]["reason"].startswith("портфельный гейт")


# ============================ сверка «журнал vs счёт» + pre-trade средства ============================

def test_execution_gap_math():
    """gap = (Δкапитала счёта) − (Δмодельного net+unrealized) от якоря; None без якоря/в paper."""
    from app.st5.service import St5Session
    s = St5Session()
    assert s._execution_gap() is None                 # нет якоря
    s.state["sandbox_active"] = True
    s.cfg.connector.account_id = "acc"
    s.exec_anchor = {"account_id": "acc", "capital": 1_000_000.0, "net": 0.0}
    s.portfolio.capital_rub = 999_448.0
    s.trades = [{"net_pnl_rub": 584.0}]
    # модель +584, факт −552 → gap = −1136 (стоимость исполнения)
    assert s._execution_gap() == -1136
    s.cfg.connector.account_id = "other"              # смена счёта → якорь невалиден
    assert s._execution_gap() is None


def test_pretrade_free_money_gate(monkeypatch):
    """Недостаток свободных средств у брокера → чистый отказ ДО ордеров (missed), без обрыва
    ноги (регресс: инцидент 02.07 «Not enough balance» с голой ногой SGU6)."""
    from app.st5.service import ST5_PAIRS, St5Portfolio, St5Session
    from app.st4 import tbank_sandbox as sb
    s = _session_with_fake_executor()
    St5Portfolio._go_cache = {pid: (1000.0, 1000.0) for pid in ST5_PAIRS}
    eng = s.engines["sber"]
    eng._open(1700000000000, -2.0, 0.0, 1.0, 28000.0, 28100.0)   # движок открыл кандидата
    assert eng.position is not None
    orders = []
    s._fake_ex.open_pair = lambda *a: orders.append(a)
    monkeypatch.setattr(s, "_make_executor", lambda pid: s._fake_ex)
    monkeypatch.setattr(sb, "free_money_rub", lambda acc: 10_000.0)   # свободно мало
    s._on_engine_opened("sber", eng, 28000.0, 28100.0)
    assert eng.position is None                       # вход откачен ЧИСТО
    assert orders == []                               # ни одного ордера не ушло
    assert any("нет ёмкости у брокера" in m["reason"] for m in s.missed)
    St5Portfolio._go_cache = {}


def test_pretrade_downsize_enters_with_fitting_units(monkeypatch):
    """Средств не хватает на полный размер, но хватает на часть → вход УМЕНЬШЕННЫМ числом
    юнитов (механика 03.07: входить пока денег хватает), а не отказ."""
    from app.st5.service import ST5_PAIRS, St5Portfolio, St5Session
    from app.st4 import tbank_sandbox as sb
    s = _session_with_fake_executor()
    St5Portfolio._go_cache = {pid: (1000.0, 1000.0) for pid in ST5_PAIRS}
    eng = s.engines["sber"]
    eng.base_lots = 2
    eng._open(1700000000000, -3.0, 0.0, 1.0, 28000.0, 28100.0)   # высокий |z| → макс. тир
    p = eng.position
    assert p is not None and p.units >= 2                        # полный размер ≥2 юнитов
    # юнит sber = 28000+28100 = 56.1к; ёмкость = 90к×0.75 = 67.5к → влезает ровно 1 юнит
    orders = []
    s._fake_ex.open_pair = lambda ls, lo, lp, ro, rp: orders.append((lo, lp))
    monkeypatch.setattr(s, "_make_executor", lambda pid: s._fake_ex)
    monkeypatch.setattr(sb, "free_money_rub", lambda acc: 90_000.0)   # ёмкость 67.5к → ровно 1 юнит (~56.1к)
    s._on_engine_opened("sber", eng, 28000.0, 28100.0)
    assert eng.position is not None
    assert eng.position.units == 1                               # урезано до вмещающегося
    assert orders == [(1, 1)]                                    # ордера ушли на 1+1 лот
    assert any("урезан по ёмкости брокера" in e["message"] for e in s.events)
    St5Portfolio._go_cache = {}


def test_periodic_reconcile_real_never_touches_account(monkeypatch):
    """БОЕВОЙ режим: осиротевшие/чужие ноги НЕ автозакрываются (на счёте могут быть ручные
    позиции оператора) — только warn-тревога. Директива 03.07: ошибки на бою очень дорогие."""
    import asyncio
    s = _session_with_fake_executor()
    s.cfg.connector.mode = "tbank_real"
    s.cfg.dedicated_account = True                     # даже с флагом — на бою не трогаем
    s._reconciled.add("sber")
    s._periodic_reconcile_every_s = 0
    s.engines["sber"].position = None
    orders = []
    s._fake_ex.broker_lots = lambda: (0, -6)
    s._fake_ex._uids = lambda: ("uid_o", "uid_p")
    s._fake_ex._post = lambda *a: orders.append(a)
    monkeypatch.setattr(s, "_make_executor", lambda pid: s._fake_ex)
    monkeypatch.setattr(s, "_ensure_uid_cache", lambda pid: pid == "sber")
    asyncio.run(s._periodic_reconcile())
    assert orders == []                                # ни одного ордера
    assert any("РЕАЛЬНОМ счёте" in e["message"] and e["kind"] == "warn" for e in s.events)
