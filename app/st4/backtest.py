"""Бэктест st4 (§14.2): оффлайн-прогон стратегии на исторических 10m-свечах.

Честный отчёт: число сделок, win-rate, средний/суммарный P&L, max просадка по equity
(включая НЕРЕАЛИЗОВАННЫЙ убыток открытой позиции — иначе 100% win-rate скрывает риск
зависших позиций), среднее проскальзывание, кривая капитала.
"""
from __future__ import annotations

from dataclasses import asdict

import pandas as pd

from .config import St4Config
from .engine import TradingEngine
from .indicators import build_band_frame
from .models import InstrumentSpec


def run_backtest(df: pd.DataFrame, cfg: St4Config,
                 spec_ord: InstrumentSpec, spec_pref: InstrumentSpec) -> dict:
    """Прогнать стратегию по df (price_a=SBRF, price_b=SBPR), вернуть метрики + кривую.

    Equity на каждом баре = balance (реализованное) + нереализованный P&L открытой
    позиции. max_drawdown_pct считается по этой equity-кривой — честный риск.
    """
    cfg = St4Config(**cfg.model_dump())
    cfg.auto_approve = True
    # Бэктест — СИМУЛЯЦИЯ на истории, всегда paper. Иначе при connector.mode=tbank_sandbox
    # движок слал бы РЕАЛЬНЫЕ ордера в песочницу (и брал цены оттуда → P&L=0). Форсим paper.
    cfg.connector.mode = "paper"
    eng = TradingEngine(cfg, spec_ord, spec_pref)

    start = cfg.paper.start_balance_rub
    equity_curve: list[dict] = []
    peak = start
    max_dd = 0.0
    for ts, row in df.iterrows():
        # объёмы ног — если df их несёт (vol_a/vol_b); иначе 0 (объёмный фильтр не сработает)
        eng.on_candles(int(ts), float(row["price_a"]), float(row["price_b"]),
                       float(row.get("vol_a", 0.0)), float(row.get("vol_b", 0.0)))
        eq = eng.balance_rub + eng.unrealized_rub()
        peak = max(peak, eq)
        dd = (peak - eq) / peak * 100 if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        equity_curve.append({"ts": int(ts), "equity": round(eq, 0)})

    trades = eng.trades
    wins = [t for t in trades if t.net_pnl_rub > 0]
    net = sum(t.net_pnl_rub for t in trades)
    gross = sum(t.gross_pnl_rub for t in trades)
    fees = sum(t.fees_rub for t in trades)
    avg_slip = (sum(t.slippage_ticks for t in trades) / len(trades)) if trades else 0.0
    open_unreal = eng.unrealized_rub()

    return {
        "bars": len(df),
        "trades": len(trades),
        "wins": len(wins),
        "win_rate_pct": round(100 * len(wins) / len(trades), 1) if trades else 0.0,
        "net_pnl_rub": round(net, 0),
        "gross_pnl_rub": round(gross, 0),
        "fees_rub": round(fees, 0),
        "avg_pnl_rub": round(net / len(trades), 0) if trades else 0.0,
        "avg_slippage_ticks": round(avg_slip, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "return_pct": round(100 * net / start, 3),
        "stops": sum(1 for t in trades if t.reason in ("stop", "time_stop")),
        "avg_bars_held": round(sum(t.bars_held for t in trades) / len(trades), 1) if trades else 0,
        "max_bars_held": max((t.bars_held for t in trades), default=0),
        "open_position": eng.state.value if eng.position else None,
        "open_unrealized_rub": round(open_unreal, 0),
        "equity_curve": equity_curve,
        "trades_detail": [_trade_dict(t) for t in trades],
    }


def _trade_dict(t) -> dict:
    d = asdict(t)
    d["state"] = t.state.value
    return d


def band_frame_for_chart(df: pd.DataFrame, cfg: St4Config) -> list[dict]:
    """Спред + полосы BB по всему df для графика вкладки (NaN → отброшены)."""
    bf = build_band_frame(df, cfg.strategy.sma_period, cfg.strategy.sigma_multiplier,
                          cfg.strategy.std_mode)
    out = []
    for ts, r in bf.iterrows():
        if pd.isna(r["sma"]) or pd.isna(r["spread"]):
            continue
        out.append({"ts": int(ts), "spread": round(float(r["spread"]), 1),
                    "sma": round(float(r["sma"]), 1), "upper": round(float(r["upper"]), 1),
                    "lower": round(float(r["lower"]), 1), "sigma": round(float(r["sigma"]), 1)})
    return out
