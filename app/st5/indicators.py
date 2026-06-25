"""Индикаторы ST5 — ядро матчасти institutional statarb.

Все формулы — по техническому ресёрчу. Принцип против look-ahead: оценка на баре t
использует только данные [..., t]; финальный торговый сигнал исполняется на следующем баре
(сдвиг — на уровне движка). Горячий путь (Kalman β, z-score, RV) — O(1) на бар. Дорогие
фильтры (ADF, Hurst, half-life) считаются по окну и кэшируются движком раз в N баров.
"""
from __future__ import annotations

import math
from collections import deque

import numpy as np


class KalmanHedge:
    """Динамический hedge ratio β через Kalman Filter (state = [α, β] random walk).

    Наблюдение: pref_t = [1, ord_t]·[α,β]ᵀ + v.  Innovation y_t = pref − H·θ⁻ — это спред,
    посчитанный по β из ПРОШЛОГО (predict до update) → out-of-sample, без look-ahead by design.
    √S_t — естественная нормировка спреда. O(1) память и время на бар.
    """

    def __init__(self, delta: float = 1e-4, obs_noise: float = 1e-3,
                 beta0: float = 1.0, alpha0: float = 0.0):
        self.theta = np.array([alpha0, beta0], dtype=float)
        self.P = np.eye(2)
        self.Q = (delta / (1.0 - delta)) * np.eye(2)   # process noise
        self.R = float(obs_noise)                      # observation noise (скаляр)
        self.ready = False

    def step(self, ord_t: float, pref_t: float) -> tuple[float, float, float]:
        """Один бар. Возвращает (beta, spread_innovation, spread_std=√S)."""
        H = np.array([1.0, ord_t])
        P_pred = self.P + self.Q
        y = pref_t - H @ self.theta            # innovation = out-of-sample спред
        S = float(H @ P_pred @ H + self.R)
        K = (P_pred @ H) / S                   # Kalman gain (2,)
        self.theta = self.theta + K * y
        self.P = P_pred - np.outer(K, H @ P_pred)
        self.ready = True
        return float(self.theta[1]), float(y), math.sqrt(max(S, 1e-12))


def rolling_ols_beta(ord_arr: np.ndarray, pref_arr: np.ndarray, window: int) -> np.ndarray:
    """Rolling OLS β = Cov(ord,pref)/Var(ord) на окне window (для бэктеста/калибровки).

    β[t] использует окно [t-w+1, t] (только прошлое+текущее). NaN до первого полного окна.
    Альтернатива Kalman; склонна к «ступенькам» на границе окна.
    """
    x = np.asarray(ord_arr, float)
    yv = np.asarray(pref_arr, float)
    n = len(x)
    beta = np.full(n, np.nan)
    if n < window:
        return beta
    cs_x = np.concatenate([[0.0], np.cumsum(x)])
    cs_y = np.concatenate([[0.0], np.cumsum(yv)])
    cs_xx = np.concatenate([[0.0], np.cumsum(x * x)])
    cs_xy = np.concatenate([[0.0], np.cumsum(x * yv)])
    for t in range(window - 1, n):
        a, b = t - window + 1, t + 1
        sx = cs_x[b] - cs_x[a]
        sy = cs_y[b] - cs_y[a]
        sxx = cs_xx[b] - cs_xx[a]
        sxy = cs_xy[b] - cs_xy[a]
        denom = window * sxx - sx * sx
        if denom != 0:
            beta[t] = (window * sxy - sx * sy) / denom
    return beta


class ZScore:
    """Онлайновый z = (spread − EMA(span)) / rolling_std(window). O(1) на бар.

    EMA рекурсивно; std — через скользящие Σx, Σx² на окне window (deque). Хранит prev_z для Δz.
    """

    def __init__(self, ema_span: int = 150, std_window: int = 150):
        self.alpha = 2.0 / (ema_span + 1.0)
        self.ema: float | None = None
        self.win: deque[float] = deque(maxlen=std_window)
        self.std_window = std_window
        self.prev_z: float | None = None

    def step(self, spread: float) -> tuple[float | None, float | None]:
        """Возвращает (z, dz). z=None пока не прогрелось окно std."""
        self.ema = spread if self.ema is None else self.alpha * spread + (1 - self.alpha) * self.ema
        self.win.append(spread)
        z = None
        if len(self.win) >= self.std_window:
            arr = np.fromiter(self.win, dtype=float)
            sd = arr.std(ddof=1)
            if sd > 1e-12:
                z = (spread - self.ema) / sd
        dz = (z - self.prev_z) if (z is not None and self.prev_z is not None) else None
        self.prev_z = z
        return z, dz


def adf_pvalue(spread_window: np.ndarray, maxlag: int = 1) -> float:
    """ADF p-value стационарности спреда (фильтр коинтеграции). p<0.05 → стационарен.

    autolag=None, фиксированный maxlag — убирает дорогой перебор по AIC (критично для real-time).
    Считается по окну, движок кэширует результат раз в N баров (дорого каждый бар).
    """
    from statsmodels.tsa.stattools import adfuller
    arr = np.asarray(spread_window, float)
    arr = arr[~np.isnan(arr)]
    if len(arr) < 20 or np.std(arr) < 1e-12:
        return 1.0   # недостаточно данных / константа → считаем нестационарным (не торгуем)
    try:
        res = adfuller(arr, maxlag=maxlag, regression="c", autolag=None)
        return float(res[1])
    except Exception:  # noqa: BLE001
        return 1.0


def hurst_rs(s: np.ndarray, min_n: int = 8, max_n: int | None = None) -> float:
    """Hurst exponent через R/S (rescaled range). H<0.5 = mean-reverting, 0.5 = random walk.

    log(R/S) ~ log(n), наклон = H. Считается по окну, кэшируется раз в N баров.
    """
    s = np.asarray(s, float)
    s = s[~np.isnan(s)]
    N = len(s)
    if N < 20:
        return 0.5
    if max_n is None:
        max_n = N // 2
    ns = np.unique(np.floor(np.logspace(np.log10(min_n), np.log10(max(min_n + 1, max_n)),
                                        12)).astype(int))
    rs = []
    for n in ns:
        if n < 2:
            continue
        m = N // n
        if m < 1:
            continue
        chunks = s[:m * n].reshape(m, n)
        Y = chunks - chunks.mean(axis=1, keepdims=True)
        Z = np.cumsum(Y, axis=1)
        R = Z.max(axis=1) - Z.min(axis=1)
        S = chunks.std(axis=1, ddof=1)
        valid = S > 1e-12
        if valid.any():
            rs.append((n, float((R[valid] / S[valid]).mean())))
    if len(rs) < 2:
        return 0.5
    ns_ = np.array([r[0] for r in rs], float)
    rs_ = np.array([r[1] for r in rs], float)
    H = float(np.polyfit(np.log(ns_), np.log(rs_), 1)[0])
    return H


def half_life(spread: np.ndarray) -> float:
    """Half-life возврата к среднему (OU/AR1): Δs = λ·s_{t-1} + c.  HL = −ln2/λ (в БАРАХ).

    λ≥0 (нет возврата) → inf. Используется для time-stop = mult × half_life.
    """
    s = np.asarray(spread, float)
    s = s[~np.isnan(s)]
    if len(s) < 10:
        return float("inf")
    s_lag = s[:-1]
    ds = np.diff(s)
    A = np.vstack([s_lag, np.ones(len(s_lag))]).T
    try:
        lam, _c = np.linalg.lstsq(A, ds, rcond=None)[0]
    except Exception:  # noqa: BLE001
        return float("inf")
    if lam >= 0:
        return float("inf")
    return float(-math.log(2) / lam)


class RVRatio:
    """Realized volatility ratio RV(short)/RV(long) по лог-доходностям цены. O(1) на бар.

    RV_k = √Σ r²  на окне k. ratio>1 — всплеск воли (риск разрыва), <1 — затишье.
    Считаем по СПРЕДУ (его воля прямо влияет на стопы); вход при ratio < rv_ratio_max.
    """

    def __init__(self, short: int = 20, long: int = 100):
        self.short = short
        self.long = long
        self.r2: deque[float] = deque(maxlen=long)
        self.prev: float | None = None

    def step(self, value: float) -> float | None:
        """value — цена/спред. Возвращает RV_short/RV_long или None пока не прогрето long-окно."""
        if self.prev is not None and self.prev != 0:
            r = math.log(abs(value) / abs(self.prev)) if (value != 0 and self.prev != 0) else 0.0
            self.r2.append(r * r)
        self.prev = value
        if len(self.r2) < self.long:
            return None
        arr = np.fromiter(self.r2, dtype=float)
        # RV как СРЕДНЕквадратичная воля на бар (√(Σr²/k)) — нормированная по длине окна, иначе
        # short⊂long и отношение не превысит 1. ratio>1 ⇔ краткосрочная воля выше долгосрочной.
        rv_long = math.sqrt(arr.mean())
        rv_short = math.sqrt(arr[-self.short:].mean())
        if rv_long < 1e-12:
            return None
        return rv_short / rv_long
