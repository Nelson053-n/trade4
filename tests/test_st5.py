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


def test_trade_limit_uses_go_factor():
    """Лимит ГО на сделку считается от ОЦЕНКИ×go_factor. Лимит 0.5% от 1М = 5000.
    risk_rub=2000, factor=4.5 → эффективно 9000 > 5000 → отказ."""
    from app.st5.service import ST5_PAIRS, St5Portfolio, St5Session
    s = St5Session()
    s.portfolio.capital_rub = 1_000_000.0
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
