"""ST5Engine — движок стратегии для ОДНОЙ пары (statarb с коинтеграцией).

Поток на бар: (ord, pref) → Kalman β → spread → z-score → [раз в N баров: пересчёт фильтров
ADF/Hurst/HL/RV] → решение (вход/частичная фиксация/полный выход/стоп). Держит одну позицию
на пару; портфельное управление (до 3 позиций, лимиты) — уровнем выше (St5Portfolio/Session).

P&L спреда: long_spread зарабатывает при РОСТЕ спреда, short — при падении.
spread = pref − β·ord. Размеры ног: pref = lots, ord = round(β·lots).
"""
from __future__ import annotations

import math

import numpy as np

from .config import St5Config
from .indicators import KalmanHedge, RVRatio, ZScore, adf_pvalue, half_life, hurst_rs
from .models import FilterState, St5Position, St5State, St5Trade


def size_multiplier(abs_z: float, cfg) -> float | None:
    """Множитель размера по |z| (1x/1.5x/2x). None если вход запрещён (|z|>z_no_entry или вне тиров)."""
    if abs_z > cfg.z_no_entry:
        return None
    for lo, hi, mult in cfg.size_tiers:
        if lo <= abs_z < hi:
            return mult
    return None


class ST5Engine:
    def __init__(self, pair: str, cfg: St5Config, base_lots: int = 1,
                 fee_per_lot: float = 2.0, half_spread_pts: float = 0.0,
                 slippage_pts: float = 0.0):
        self.pair = pair
        self.cfg = cfg
        s = cfg.strategy
        self.base_lots = base_lots
        self.fee_per_lot = fee_per_lot          # комиссия за лот (одна нога)
        self.half_spread = half_spread_pts      # половина bid-ask в пунктах (на ногу)
        self.slippage = slippage_pts            # проскальзывание в пунктах
        self.kalman = KalmanHedge(s.kalman_delta, s.kalman_obs_noise)
        self.zscore = ZScore(s.z_ema_span, s.z_std_window)
        self.rv = RVRatio(s.rv_short, s.rv_long)
        self.spread_buf: list[float] = []       # для ADF/Hurst/HL (окно)
        self.filt = FilterState()
        self.position: St5Position | None = None
        self.trades: list[St5Trade] = []
        self.last_z: float | None = None
        self.last_beta: float = 1.0
        self.last_spread: float = 0.0
        self._bars = 0

    # ---------- фильтры (дорого → раз в N баров) ----------
    def _recalc_filters(self) -> None:
        s = self.cfg.strategy
        buf = np.asarray(self.spread_buf[-s.adf_window:], float)
        self.filt.adf_p = adf_pvalue(buf[-s.adf_window:]) if len(buf) >= 50 else 1.0
        hbuf = np.asarray(self.spread_buf[-s.hurst_window:], float)
        self.filt.hurst = hurst_rs(hbuf) if len(hbuf) >= 50 else 0.5
        hl = half_life(np.asarray(self.spread_buf[-s.adf_window:], float))
        # clamp: HL нестабилен на коротком/шумном окне (может дать <1 бара → time-stop закроет
        # мгновенно). Минимум 5 баров, максимум — окно/2 (дальше нет смысла держать).
        if hl != float("inf"):
            hl = max(5.0, min(hl, s.adf_window / 2))
        self.filt.half_life = hl
        self.filt.cointegrated = self.filt.adf_p < s.adf_p_enter
        self.filt.mean_reverting = s.hurst_min < self.filt.hurst < s.hurst_max
        self.filt.bars_since_calc = 0

    # ---------- исполнение (реалистичная модель) ----------
    def _fill_price(self, ref: float, is_buy: bool) -> float:
        """Цена исполнения с half-spread + slippage против нас."""
        adj = self.half_spread + self.slippage
        return ref + adj if is_buy else ref - adj

    def _pair_fee(self, lots: int) -> float:
        return 2 * lots * self.fee_per_lot   # две ноги

    # ---------- основной шаг ----------
    def step(self, ts: int, ord_px: float, pref_px: float, ts_local_min: int | None = None) -> St5Trade | None:
        """Один бар. ts_local_min — минута в торговом дне (для временных окон); None = без фильтра.

        Возвращает St5Trade при закрытии (полном/частичном), иначе None.
        """
        s = self.cfg.strategy
        self._bars += 1
        beta, spread, spread_std = self.kalman.step(ord_px, pref_px)
        self.last_beta = beta
        # Kalman warmup: первые kalman_warmup баров β ещё не сошёлся (мусорный спред, особенно
        # на парах с разным масштабом цен типа TATN/TATP 10×). НЕ кормим ими фильтры/z/буфер —
        # иначе один выброс отравляет std/ADF/Hurst надолго (z=36 артефакт).
        if self._bars <= s.kalman_warmup:
            self.last_spread = spread
            return None
        self.last_spread = spread
        self.spread_buf.append(spread)
        if len(self.spread_buf) > max(s.adf_window, s.hurst_window) + 50:
            self.spread_buf.pop(0)
        z, dz = self.zscore.step(spread)
        cur_rv = self.rv.step(spread)            # RV-ratio (раз за бар; None пока не прогрето)
        # рыночные фильтры (дорого) — пересчёт раз в N баров
        self.filt.bars_since_calc += 1
        if self.filt.bars_since_calc >= s.filter_recalc_bars or self._bars == s.adf_window:
            if len(self.spread_buf) >= 50:
                self._recalc_filters()
        # RV-режим (дёшево, каждый бар)
        self.filt.rv_ratio = cur_rv if cur_rv is not None else 0.0
        self.filt.calm_regime = (cur_rv is not None and cur_rv < s.rv_ratio_max)

        if z is None:
            self.last_z = z
            return None

        result: St5Trade | None = None
        if self.position is not None:
            self.position.bars_held += 1
            result = self._manage_position(ts, z, spread, ord_px, pref_px)
        elif self._can_enter(z, dz, ts_local_min):
            self._open(ts, z, spread, beta, ord_px, pref_px)

        self.last_z = z
        return result

    # ---------- вход ----------
    def _can_enter(self, z: float, dz: float | None, ts_local_min: int | None) -> bool:
        s = self.cfg.strategy
        if not self.filt.entry_allowed():
            return False
        if self._in_no_entry_window(ts_local_min):
            return False
        az = abs(z)
        if az <= s.z_entry or az > s.z_no_entry:
            return False
        if size_multiplier(az, s) is None:
            return False
        if s.require_dz_confirm:
            if dz is None or self.last_z is None:
                return False
            # схождение: |z| уменьшается, Δz в сторону нуля
            if abs(z) >= abs(self.last_z):
                return False
            if z > 0 and dz >= 0:   # short-кандидат, но z растёт
                return False
            if z < 0 and dz <= 0:   # long-кандидат, но z падает
                return False
        return True

    def _in_no_entry_window(self, m: int | None) -> bool:
        """Временные ограничения (минута в дне). Упрощённо для FORTS: основная 10:00–18:50,
        клиринг 14:00–14:05, вечерняя до 23:50. None → фильтр выключен (бэктест без TZ)."""
        if m is None:
            return False
        s = self.cfg.strategy
        OPEN = 10 * 60          # 10:00
        CLEARING = 14 * 60      # 14:00
        EVE_CLOSE = 23 * 60 + 50
        if m < OPEN + s.no_entry_open_min:
            return True
        if CLEARING - s.no_entry_before_clearing_min <= m < CLEARING + 5 + s.no_entry_after_clearing_min:
            return True
        if m >= EVE_CLOSE - s.no_entry_before_close_min:
            return True
        return False

    def _open(self, ts: int, z: float, spread: float, beta: float,
              ord_px: float, pref_px: float) -> None:
        s = self.cfg.strategy
        mult = size_multiplier(abs(z), s) or 1.0
        lots = max(1, int(round(self.base_lots * mult)))
        state = St5State.LONG_SPREAD if z < 0 else St5State.SHORT_SPREAD
        # цены исполнения: long_spread = buy pref + sell ord; short = наоборот
        buy_pref = (state == St5State.LONG_SPREAD)
        pref_fill = self._fill_price(pref_px, buy_pref)
        ord_fill = self._fill_price(ord_px, not buy_pref)
        hl = self.filt.half_life
        self.position = St5Position(
            pair=self.pair, state=state, entry_ts=ts, entry_z=z, entry_spread=spread,
            entry_beta=beta, lots=lots, entry_lots=lots, ord_entry=ord_fill, pref_entry=pref_fill,
            half_life=hl, fees_rub=self._pair_fee(lots))

    # ---------- ведение позиции ----------
    def _manage_position(self, ts: int, z: float, spread: float,
                         ord_px: float, pref_px: float) -> St5Trade | None:
        s = self.cfg.strategy
        p = self.position
        az = abs(z)
        # 1) hard-стопы (закрывают всё)
        if az > s.z_stop:
            return self._close(ts, z, spread, ord_px, pref_px, "z_stop")
        if p.half_life != float("inf") and p.bars_held > s.half_life_stop_mult * p.half_life:
            return self._close(ts, z, spread, ord_px, pref_px, "time_stop")
        if self.filt.adf_p > s.adf_p_break:
            return self._close(ts, z, spread, ord_px, pref_px, "adf_break")
        # 2) полный выход остатка
        if az < s.z_exit_full:
            return self._close(ts, z, spread, ord_px, pref_px, "exit")
        # 3) частичная фиксация 50% при |z| < z_take_partial
        if (not p.partial_done) and az < s.z_take_partial and p.lots >= 2:
            return self._take_partial(ts, z, spread, ord_px, pref_px)
        return None

    def _leg_exit_prices(self, ord_px: float, pref_px: float) -> tuple[float, float]:
        """Цены выхода ног (обратные сторонам входа)."""
        p = self.position
        sell_pref = (p.state == St5State.LONG_SPREAD)   # лонг закрываем продажей префа
        pref_fill = self._fill_price(pref_px, not sell_pref)
        ord_fill = self._fill_price(ord_px, sell_pref)
        return ord_fill, pref_fill

    def _spread_pnl(self, exit_spread: float, lots: int) -> float:
        """P&L по спреду: long выигрывает при росте, short — при падении. ×lots."""
        p = self.position
        d = exit_spread - p.entry_spread
        sign = 1.0 if p.state == St5State.LONG_SPREAD else -1.0
        return sign * d * lots

    def _take_partial(self, ts: int, z: float, spread: float,
                      ord_px: float, pref_px: float) -> St5Trade:
        p = self.position
        close_lots = max(1, int(p.lots * self.cfg.strategy.partial_take_frac))
        ord_x, pref_x = self._leg_exit_prices(ord_px, pref_px)
        exit_spread = pref_x - p.entry_beta * ord_x
        gross = self._spread_pnl(exit_spread, close_lots)
        fee = self._pair_fee(close_lots)
        net = gross - fee
        p.lots -= close_lots
        p.partial_done = True
        p.realized_rub += net
        p.fees_rub += fee
        tr = St5Trade(pair=self.pair, state=p.state, entry_ts=p.entry_ts, exit_ts=ts,
                      entry_z=p.entry_z, exit_z=z, entry_spread=p.entry_spread, exit_spread=exit_spread,
                      lots=close_lots, gross_pnl_rub=gross, fees_rub=fee, net_pnl_rub=net,
                      reason="take_partial", bars_held=p.bars_held, entry_beta=p.entry_beta)
        self.trades.append(tr)
        return tr

    def _close(self, ts: int, z: float, spread: float,
               ord_px: float, pref_px: float, reason: str) -> St5Trade:
        p = self.position
        ord_x, pref_x = self._leg_exit_prices(ord_px, pref_px)
        exit_spread = pref_x - p.entry_beta * ord_x
        gross = self._spread_pnl(exit_spread, p.lots)
        fee = self._pair_fee(p.lots)
        net = gross - fee
        tr = St5Trade(pair=self.pair, state=p.state, entry_ts=p.entry_ts, exit_ts=ts,
                      entry_z=p.entry_z, exit_z=z, entry_spread=p.entry_spread, exit_spread=exit_spread,
                      lots=p.lots, gross_pnl_rub=gross, fees_rub=fee, net_pnl_rub=net,
                      reason=reason, bars_held=p.bars_held, entry_beta=p.entry_beta)
        self.trades.append(tr)
        self.position = None
        return tr

    def unrealized_rub(self) -> float:
        """Нереализованный P&L открытой позиции по последнему спреду."""
        if self.position is None or self.last_z is None:
            return 0.0
        return self._spread_pnl(self.last_spread, self.position.lots)
