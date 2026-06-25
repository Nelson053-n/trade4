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


def test_portfolio_limits_gate():
    """Портфельный гейт: лимит на сделку, число позиций, ≤1 на эмитента."""
    from app.st5.service import ST5_PAIRS, St5Session
    from app.st5.models import St5Position, St5State
    s = St5Session()
    s.portfolio.capital_rub = 1_000_000.0
    # лимит на сделку 0.5% = 5000: нотионал 4000 проходит, 6000 — нет
    ok, _ = s.portfolio.can_open("sber", "SBER", 4000.0, s.engines, ST5_PAIRS)
    assert ok
    ok2, reason = s.portfolio.can_open("sber", "SBER", 6000.0, s.engines, ST5_PAIRS)
    assert not ok2 and "сделк" in reason
    # открыта позиция по эмитенту SBER → вход в sber запрещён (≤1 на эмитента)
    s.engines["sber"].position = St5Position(
        pair="sber", state=St5State.LONG_SPREAD, entry_ts=0, entry_z=-2.5, entry_spread=0.0,
        entry_beta=1.0, lots=10, entry_lots=10, ord_entry=100.0, pref_entry=100.0, half_life=10)
    ok3, reason3 = s.portfolio.can_open("sber", "SBER", 1000.0, s.engines, ST5_PAIRS)
    assert not ok3 and "эмитент" in reason3
    s.engines["sber"].position = None
    # HALT блокирует всё
    s.portfolio.halt("тест")
    ok4, _ = s.portfolio.can_open("sber", "SBER", 1000.0, s.engines, ST5_PAIRS)
    assert not ok4


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
