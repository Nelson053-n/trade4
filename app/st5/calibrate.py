"""Walk-forward автокалибровка ST5 (60 дней train / 20 торговля).

По ресёрчу: грубая сетка параметров (не мелкая — против переобучения), целевая функция —
робастный Sharpe на OOS-сегментах. Результат пишется в STAGING (не применяется
автоматически — боевые параметры меняются только с явным подтверждением оператора).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .backtest import run_backtest
from .config import St5Config


def walk_forward(df: pd.DataFrame, base_cfg: St5Config, pair: str = "cal",
                 train_bars: int = 60 * 24 * 6, trade_bars: int = 20 * 24 * 6) -> dict:
    """Walk-forward по грубой сетке. df: price_a/price_b, индекс ts.

    Окна в БАРАХ (для 10m: 60д ≈ 60*24*6, но реально баров меньше — клампим под длину df).
    Возвращает {best_params, oos_sharpe, segments, grid_results}.
    """
    n = len(df)
    # клампим окна под реальную длину истории
    train_bars = min(train_bars, n // 3)
    trade_bars = min(trade_bars, n // 6)
    if train_bars < 200 or trade_bars < 50:
        return {"error": f"мало данных для walk-forward: {n} баров"}

    # ГРУБАЯ сетка (3-4 значения на параметр — против переобучения)
    grid = []
    for z_entry in (2.0, 2.25, 2.5):
        for z_stop in (4.0, 4.25, 5.0):
            for hurst_max in (0.55, 0.60):
                grid.append({"z_entry": z_entry, "z_stop": z_stop, "hurst_max": hurst_max})

    # walk-forward: на каждом train-окне выбираем лучшие параметры, торгуем на следующем trade-окне
    seg_results = []
    start = 0
    oos_pnls = []
    while start + train_bars + trade_bars <= n:
        train = df.iloc[start:start + train_bars]
        trade = df.iloc[start + train_bars:start + train_bars + trade_bars]
        # подбор на train
        best, best_sharpe = None, -1e9
        for params in grid:
            c = St5Config(**base_cfg.model_dump())
            for k, v in params.items():
                setattr(c.strategy, k, v)
            c.strategy.require_dz_confirm = False
            m = run_backtest(train, c, pair=pair, base_lots=10)
            if m.trades >= 2 and m.sharpe > best_sharpe:
                best_sharpe, best = m.sharpe, params
        if best is None:
            start += trade_bars
            continue
        # OOS-торговля на trade-окне выбранными параметрами
        c = St5Config(**base_cfg.model_dump())
        for k, v in best.items():
            setattr(c.strategy, k, v)
        c.strategy.require_dz_confirm = False
        m_oos = run_backtest(trade, c, pair=pair, base_lots=10)
        seg_results.append({"params": best, "oos_net": m_oos.net_pnl_rub,
                            "oos_trades": m_oos.trades, "oos_sharpe": round(m_oos.sharpe, 2)})
        oos_pnls.append(m_oos.net_pnl_rub)
        start += trade_bars

    if not seg_results:
        return {"error": "ни один сегмент не дал сделок"}

    # итоговые параметры = медиана/мода по сегментам (стабильность = здоровье стратегии)
    from collections import Counter
    z_entries = Counter(s["params"]["z_entry"] for s in seg_results)
    z_stops = Counter(s["params"]["z_stop"] for s in seg_results)
    hursts = Counter(s["params"]["hurst_max"] for s in seg_results)
    best_params = {
        "z_entry": z_entries.most_common(1)[0][0],
        "z_stop": z_stops.most_common(1)[0][0],
        "hurst_max": hursts.most_common(1)[0][0],
    }
    oos_arr = np.array(oos_pnls, float)
    oos_sharpe = float(oos_arr.mean() / oos_arr.std(ddof=1) * np.sqrt(len(oos_arr))) \
        if len(oos_arr) >= 2 and oos_arr.std(ddof=1) > 1e-9 else 0.0
    # стабильность: доля сегментов, где выбраны итоговые параметры (выше = надёжнее)
    stable = sum(1 for s in seg_results if s["params"] == best_params) / len(seg_results)
    return {
        "best_params": best_params,
        "oos_total_net": round(float(oos_arr.sum()), 0),
        "oos_sharpe": round(oos_sharpe, 2),
        "segments": len(seg_results),
        "stability": round(stable, 2),
        "seg_results": seg_results,
    }
