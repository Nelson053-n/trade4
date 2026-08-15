"""Портфельный стенд ST9 с БОЕВЫМ сайзингом и стопом просадки капитала.

Зачем отдельно от `backtest.py`. Тот гоняет оси НЕЗАВИСИМО с фиксированным нотионалом
(`notional=400_000`), а бой работает иначе:

  * размер считается от ТЕКУЩЕГО капитала (`go_target_pct` % ГО / число осей ÷ ГО на лот),
    т.е. есть ОБРАТНАЯ СВЯЗЬ: P&L → капитал → размер следующей позиции;
  * USDRUBF сайзится по РИСКУ (`risk_per_trade_rub / (atr_mult × ATR × pv)`), не по плечу;
  * стоп просадки капитала смотрит на ПОРТФЕЛЬ и при срабатывании закрывает ВСЕ оси.

Из-за этого симуляция на кривых `backtest.run()` не воспроизводила боевое срабатывание
стопа 14.08 (давала 0 при любом пороге за 3.8 года) — сравнение было некорректным по
построению, а не «стоп не нужен». Здесь оси идут по ОБЩЕМУ таймлайну, капитал общий.

ГО берётся долей нотионала (замер GetFuturesMargin 15.08: USDRUBF 14.9%, GLDRUBF 12.4%,
IMOEXF 10.5%) и масштабируется с ценой — НЕ константой `go_frac=0.044`, которая занижает
реальное ГО в 2.4-3.4× и завышала бы плечо.
"""
from __future__ import annotations

import datetime as _dt

from .backtest import PV, load_bars, swap_rates
from .engine import St9Engine

# доля ГО от нотионала (GetFuturesMargin, замер 15.08.2026)
GO_FRAC_REAL = {"USDRUBF": 0.1488, "GLDRUBF": 0.1238, "IMOEXF": 0.1046}

# кэш загруженной истории: свип по порогам гоняет одни и те же бары десятки раз
_BARS_CACHE: dict = {}
_SWAP_CACHE: dict = {}


class Axis:
    """Одна ось: движок + бары + курсор по таймлайну."""

    def __init__(self, secid, don_enter, don_exit, atr_mult, bars,
                 fee_pct=0.05, risk_per_trade=0.0, allow_short=True):
        self.secid = secid
        self.bars = bars
        self.atr_mult = atr_mult
        self.risk_per_trade = risk_per_trade
        self.pv = PV[secid]
        self.eng = St9Engine(secid=secid, don_enter=don_enter, don_exit=don_exit,
                             atr_mult=atr_mult, atr_period=14, pv=self.pv,
                             fee_pct_notional=fee_pct, allow_short=allow_short)
        self.by_ts = {b.ts: b for b in bars}
        self.last_px = None
        self.realized = 0.0
        self.trades = []

    def lots_for(self, px, capital, go_pct, n_axes, atr):
        """Боевой сайзинг: риск-режим приоритетнее плеча (канон service._entry_lots)."""
        if px <= 0:
            return 0
        if self.risk_per_trade > 0:
            if not atr or atr <= 0:
                return 0                       # ATR не прогрет — на риск-оси размер нечем считать
            risk_per_lot = self.atr_mult * atr * self.pv
            return max(1, int(self.risk_per_trade / risk_per_lot)) if risk_per_lot > 0 else 0
        if go_pct > 0 and capital > 0:
            go_per_axis = capital * (go_pct / 100.0) / max(1, n_axes)
            go_lot = px * self.pv * GO_FRAC_REAL.get(self.secid, 0.12)
            if go_lot > 0:
                return max(1, int(go_per_axis / go_lot))
        return 0

    def unrealized(self):
        if self.eng.position is None or self.last_px is None:
            return 0.0
        return self.eng.unrealized_rub(self.last_px)

    def equity(self):
        return self.realized + self.unrealized()


def run_portfolio(axes_cfg, start_capital=500_000.0, go_target_pct=15.0,
                  dd_stop_pct=18.0, fee_pct=0.05, slip_pct=0.11, use_swaps=True,
                  days=1400, metric="mtm", reset_peak_on_halt=True, verbose=False):
    """Портфельный прогон. Возвращает свод + список срабатываний стопа.

    metric: 'mtm' — guard смотрит капитал с плавающей прибылью (как в бою);
            'realized' — только реализованное (проверяемая альтернатива).
    """
    axes = []
    for cfg in axes_cfg:
        # КЭШ: свип по порогам зовёт run_portfolio десятки раз на ОДНИХ И ТЕХ ЖЕ данных.
        # Без кэша 1400 дней × 3 оси качались заново каждый раз (CPU 0.7% — всё время
        # в сети, прогон растягивался на часы).
        key = (cfg["secid"], days)
        if key not in _BARS_CACHE:
            _BARS_CACHE[key] = load_bars(cfg["secid"], days=days)
        bars = _BARS_CACHE[key]
        sw = None
        if use_swaps:
            if cfg["secid"] not in _SWAP_CACHE:
                try:
                    _SWAP_CACHE[cfg["secid"]] = swap_rates(cfg["secid"])
                except Exception:  # noqa: BLE001  фандинг не критичен для метрики стопа
                    _SWAP_CACHE[cfg["secid"]] = None
            sw = _SWAP_CACHE[cfg["secid"]]
        ax = Axis(cfg["secid"], cfg["don_enter"], cfg["don_exit"], cfg["atr_mult"],
                  bars, fee_pct=fee_pct, risk_per_trade=cfg.get("risk_per_trade_rub", 0.0))
        ax.swaps = sw
        ax.prev_day = None
        axes.append(ax)

    n_axes = len(axes)
    ts_all = sorted({t for ax in axes for t in ax.by_ts})
    peak = start_capital
    halted = False
    breach = 0
    fires = []
    curve = []

    for ts in ts_all:
        # ---- прогон баров этого момента по всем осям ----
        for ax in axes:
            b = ax.by_ts.get(ts)
            if b is None:
                continue
            ax.last_px = b.c
            # фандинг: раз в календарный день удержания
            if ax.swaps and ax.eng.position is not None:
                day = _dt.datetime.fromtimestamp(b.ts / 1000).date().isoformat()
                if ax.prev_day is not None and day != ax.prev_day:
                    sr = ax.swaps.get(day)
                    if sr is not None:
                        p = ax.eng.position
                        f = -sr * ax.pv * p.lots if p.side == "long" else sr * ax.pv * p.lots
                        p.fees_rub -= f
                ax.prev_day = day
            elif ax.swaps:
                ax.prev_day = _dt.datetime.fromtimestamp(b.ts / 1000).date().isoformat()

            capital = start_capital + sum(a.equity() for a in axes)
            atr_now = ax.eng._atr()
            lots_pre = ax.lots_for(b.c, capital, go_target_pct, n_axes, atr_now)
            sig = ax.eng.step(b, max(0, lots_pre))
            if not sig:
                continue
            px = sig["px"]
            if sig["act"] in ("close", "reverse"):
                d = 1 if ax.eng.position.side == "long" else -1
                tr = ax.eng.close(px * (1 - slip_pct / 100 * d), b.ts, sig["reason"])
                ax.realized += tr.net_pnl_rub
                ax.trades.append(tr)
            if sig["act"] in ("open", "reverse") and not halted:
                side = sig["new_side"]
                lots = ax.lots_for(px, capital, go_target_pct, n_axes, sig.get("atr"))
                if lots > 0:
                    d = 1 if side == "long" else -1
                    ax.eng.open(side, px * (1 + slip_pct / 100 * d), lots, b.ts, sig["atr"])

        # ---- guard просадки капитала (канон service._check_capital_dd) ----
        eq_mtm = sum(a.equity() for a in axes)
        eq_real = sum(a.realized for a in axes)
        cap_now = start_capital + (eq_mtm if metric == "mtm" else eq_real)
        curve.append((ts, start_capital + eq_mtm, start_capital + eq_real))
        if dd_stop_pct <= 0 or cap_now <= 0:
            continue
        if not (peak > 0 and cap_now > peak * 1.15):     # защита пика от выброса
            peak = max(peak, cap_now)
        if halted:
            continue
        floor = peak * (1 - dd_stop_pct / 100.0)
        if cap_now >= floor:
            breach = 0
            continue
        breach += 1
        if breach < 2:                                    # подтверждение 2 тика подряд
            continue
        dd = (1 - cap_now / peak) * 100
        pos_open = sum(1 for a in axes if a.eng.position)
        pnl_open = sum(a.unrealized() for a in axes)
        # flat всех осей
        for a in axes:
            if a.eng.position and a.last_px is not None:
                d = 1 if a.eng.position.side == "long" else -1
                tr = a.eng.close(a.last_px * (1 - slip_pct / 100 * d), ts, "dd_stop")
                a.realized += tr.net_pnl_rub
                a.trades.append(tr)
        fires.append({"ts": ts, "date": _dt.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d"),
                      "dd_pct": round(dd, 1), "capital": round(cap_now), "peak": round(peak),
                      "positions_closed": pos_open, "unrealized_at_stop": round(pnl_open)})
        if verbose:
            print(f"    🚨 {fires[-1]['date']}: DD {dd:.1f}%, закрыто {pos_open} поз., "
                  f"плавающих {pnl_open:+,.0f}₽")
        breach = 0
        if reset_peak_on_halt:
            peak = cap_now                                # оператор снимает стоп → пик сбрасывается
        else:
            halted = True

    final_mtm = start_capital + sum(a.equity() for a in axes)
    final_real = start_capital + sum(a.realized for a in axes)
    max_dd = 0.0
    pk = 0.0
    for _, c_mtm, _ in curve:
        pk = max(pk, c_mtm)
        if pk > 0:
            max_dd = max(max_dd, (1 - c_mtm / pk) * 100)
    return {"fires": fires, "n_fires": len(fires), "final_mtm": round(final_mtm),
            "final_realized": round(final_real), "max_dd_pct": round(max_dd, 1),
            "trades": sum(len(a.trades) for a in axes), "curve": curve,
            "span_days": round((ts_all[-1] - ts_all[0]) / 1000 / 86400)}
