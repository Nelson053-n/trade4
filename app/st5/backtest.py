"""Бэктест ST5 на исторических данных (df: price_a=ord, price_b=pref).

Прогоняет ST5Engine по барам, считает метрики: net P&L, win-rate, Sharpe (по сделкам),
profit factor, maxDD по equity-кривой с нереализованным P&L. Реалистичное исполнение
(комиссии, half-spread, slippage) задаётся параметрами движка.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .config import St5Config
from .engine import ST5Engine
from .models import St5Metrics


def run_backtest(df: pd.DataFrame, cfg: St5Config, pair: str = "test",
                 base_lots: int = 1, fee_per_lot: float = 2.0,
                 half_spread_pts: float = 0.5, slippage_pts: float = 0.0,
                 start_balance: float = 1_000_000.0) -> St5Metrics:
    """df с колонками price_a (ord), price_b (pref), индекс — ts (ms)."""
    eng = ST5Engine(pair, cfg, base_lots=base_lots, fee_per_lot=fee_per_lot,
                    half_spread_pts=half_spread_pts, slippage_pts=slippage_pts)
    m = St5Metrics()
    balance = start_balance
    peak = start_balance
    equity_curve = []
    for ts, row in df.iterrows():
        tr = eng.step(int(ts), float(row["price_a"]), float(row["price_b"]))
        if tr is not None:
            balance += tr.net_pnl_rub
        eq = balance + eng.unrealized_rub()
        peak = max(peak, eq)
        dd = (peak - eq) / peak * 100 if peak > 0 else 0.0
        m.max_drawdown_pct = max(m.max_drawdown_pct, dd)
        equity_curve.append((int(ts), round(eq, 0)))

    trades = eng.trades
    m.bars = len(df)
    m.trades = len(trades)
    m.wins = sum(1 for t in trades if t.net_pnl_rub > 0)
    m.net_pnl_rub = sum(t.net_pnl_rub for t in trades)
    m.gross_pnl_rub = sum(t.gross_pnl_rub for t in trades)
    m.fees_rub = sum(t.fees_rub for t in trades)
    m.equity_curve = equity_curve[-400:]
    # причины
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    m.reasons = reasons
    # profit factor = сумма прибылей / |сумма убытков|
    gains = sum(t.net_pnl_rub for t in trades if t.net_pnl_rub > 0)
    losses = -sum(t.net_pnl_rub for t in trades if t.net_pnl_rub < 0)
    m.profit_factor = (gains / losses) if losses > 1e-9 else (float("inf") if gains > 0 else 0.0)
    # Sharpe по P&L сделок (annualization опускаем — относительная мера)
    if len(trades) >= 2:
        pnls = np.array([t.net_pnl_rub for t in trades], float)
        sd = pnls.std(ddof=1)
        m.sharpe = float(pnls.mean() / sd * math.sqrt(len(pnls))) if sd > 1e-9 else 0.0
    return m


def metrics_summary(m: St5Metrics) -> str:
    return (f"сделок {m.trades} win {m.win_rate_pct:.0f}% net {m.net_pnl_rub:+.0f}₽ "
            f"PF {m.profit_factor:.2f} Sharpe {m.sharpe:.2f} maxDD {m.max_drawdown_pct:.1f}% "
            f"| {m.reasons}")
