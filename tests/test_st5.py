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
