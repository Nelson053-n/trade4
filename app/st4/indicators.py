"""Bollinger Bands на спреде + сборка свечей спреда (§7, §8).

BollingerBands — кольцевой буфер на SmaPeriod значений SpreadClose; на каждый закрытый
спред-бар отдаёт SMA/σ/полосы. SpreadBuilder синхронизирует свечи двух ног по времени
и формирует бар спреда только когда обе ноги закрылись (пропуски не подставляются).

Все расчёты — по закрытым свечам (no repaint).
"""
from __future__ import annotations

import math
from collections import deque

import pandas as pd

from .models import BandReading, SpreadBar


class BollingerBands:
    """BB(period, k·σ) на потоке SpreadClose (§8).

    std_mode: Population → делим на N, Sample → на (N−1). is_ready, пока буфер не полон.
    """

    def __init__(self, period: int = 200, sigma_mult: float = 2.0,
                 std_mode: str = "Population") -> None:
        self.period = period
        self.k = sigma_mult
        self.std_mode = std_mode
        self._buf: deque[float] = deque(maxlen=period)

    @property
    def is_ready(self) -> bool:
        return len(self._buf) >= self.period

    def _sma_sigma(self) -> tuple[float, float]:
        n = len(self._buf)
        mean = math.fsum(self._buf) / n
        ddof = 0 if self.std_mode == "Population" else 1
        denom = n - ddof
        if denom <= 0:
            return mean, 0.0
        var = math.fsum((x - mean) ** 2 for x in self._buf) / denom
        return mean, math.sqrt(var)

    def update(self, ts: int, spread: float) -> BandReading:
        """Добавить новое значение спреда, вернуть срез полос (после добавления)."""
        self._buf.append(spread)
        if not self.is_ready:
            return BandReading(ts=ts, spread=spread, sma=float("nan"), sigma=float("nan"),
                               upper=float("nan"), lower=float("nan"), is_ready=False)
        sma, sigma = self._sma_sigma()
        return BandReading(ts=ts, spread=spread, sma=sma, sigma=sigma,
                           upper=sma + self.k * sigma, lower=sma - self.k * sigma,
                           is_ready=True)

    def warmup(self, spreads: list[float]) -> None:
        """Прогрев историей: заполнить буфер без генерации сигналов."""
        for s in spreads:
            self._buf.append(s)


class VolumeAverage:
    """Потоковая SMA объёма бара спреда на кольцевом буфере period значений.

    Аналог BollingerBands по структуре буфера; даёт скользящее среднее объёма для
    объёмного фильтра входа. is_ready — пока буфер не полон (как у BB).
    """

    def __init__(self, period: int = 200) -> None:
        self.period = period
        self._buf: deque[float] = deque(maxlen=period)

    @property
    def is_ready(self) -> bool:
        return len(self._buf) >= self.period

    def update(self, volume: float) -> float:
        """Добавить объём бара, вернуть SMA по текущему окну (NaN, пока пуст)."""
        self._buf.append(volume)
        if not self._buf:
            return float("nan")
        return math.fsum(self._buf) / len(self._buf)


class SpreadBuilder:
    """Синхронизация свечей двух ног и построение баров спреда (§7).

    spread(t) = Close(SBPR, t) − Close(SBRF, t). Бар формируется только когда обе ноги
    для интервала закрылись; пропуск одной ноги → бар не строится (значение не подставляем).
    Объёмы ног (если переданы) суммируются в SpreadBar.volume для объёмного фильтра.
    """

    def __init__(self) -> None:
        self._ord: dict[int, float] = {}        # ts → Close(SBRF)
        self._pref: dict[int, float] = {}       # ts → Close(SBPR)
        self._vol_ord: dict[int, float] = {}    # ts → Volume(SBRF)
        self._vol_pref: dict[int, float] = {}   # ts → Volume(SBPR)

    def add_ordinary(self, ts: int, close: float, volume: float = 0.0) -> SpreadBar | None:
        self._ord[ts] = close
        self._vol_ord[ts] = volume
        return self._try(ts)

    def add_preferred(self, ts: int, close: float, volume: float = 0.0) -> SpreadBar | None:
        self._pref[ts] = close
        self._vol_pref[ts] = volume
        return self._try(ts)

    def _try(self, ts: int) -> SpreadBar | None:
        if ts in self._ord and ts in self._pref:
            o, p = self._ord[ts], self._pref[ts]
            vol = self._vol_ord.get(ts, 0.0) + self._vol_pref.get(ts, 0.0)
            return SpreadBar(ts=ts, close_ord=o, close_pref=p, spread=p - o, volume=vol)
        return None


def spread_series(df: pd.DataFrame) -> pd.Series:
    """df['price_a']=SBRF, df['price_b']=SBPR → серия спреда SBPR−SBRF (по ts)."""
    s = df["price_b"] - df["price_a"]
    s.index.name = "ts"
    return s


def build_band_frame(df: pd.DataFrame, period: int, sigma_mult: float,
                     std_mode: str = "Population") -> pd.DataFrame:
    """Векторный расчёт BB по всему df — для бэктеста/прогрева/графика (эталон pandas).

    Возвращает DataFrame с колонками spread, sma, sigma, upper, lower (NaN на прогреве).
    Совпадает с потоковым BollingerBands.update (тот же ddof, то же окно).
    """
    spread = spread_series(df)
    ddof = 0 if std_mode == "Population" else 1
    sma = spread.rolling(period).mean()
    sigma = spread.rolling(period).std(ddof=ddof)
    return pd.DataFrame({
        "spread": spread,
        "sma": sma,
        "sigma": sigma,
        "upper": sma + sigma_mult * sigma,
        "lower": sma - sigma_mult * sigma,
    })
