"""Скан пар обычка/преф на FORTS: бэктест st4-движка по каждой паре.

Кандидаты — эмитенты, у которых на FORTS есть фьючерсы и на обыкновенные, и на
привилегированные акции (Сбер, Татнефть, Сургутнефтегаз). Наличие серий проверяется
по справочнику ISS — отсутствующие пары пропускаются, список можно переопределить
флагом --pairs. По каждой паре: история 10m за период, run_backtest текущим конфигом,
метрики целиком и по половинам периода (грубая проверка устойчивости), ADF p-value
спреда (стационарность). Результат — таблица в stdout + JSON для веб-отчёта + CSV.

ВНИМАНИЕ: бэктест идёт по ближайшей ликвидной серии — на периодах сильно длиннее
жизни серии (>~90 дней) история тонкая и результат смещён (склейки серий нет).

  python -m app.st4.scan_pairs --days 60
  python -m app.st4.scan_pairs --days 90 --stop-sigma 4 --pairs SBRF:SBPR
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import data_feed as feed
from .backtest import run_backtest
from .config import St4Config
from .indicators import spread_series
from .models import Role

# базовые активы FORTS с фьючерсами на обычку и преф одного эмитента
DEFAULT_PAIRS: list[tuple[str, str]] = [
    ("SBRF", "SBPR"),   # Сбербанк
    ("TATN", "TATP"),   # Татнефть
    ("SNGR", "SNGP"),   # Сургутнефтегаз
]
OUT_JSON = Path(__file__).resolve().parent.parent.parent / "out" / "st4_scan_pairs.json"


def _adf_pvalue(spread) -> float | None:
    """ADF p-value спреда (меньше — стационарнее). None, если ряд вырожден."""
    try:
        from statsmodels.tsa.stattools import adfuller
        s = spread.dropna()
        if len(s) < 50 or float(s.std()) == 0.0:
            return None
        return float(adfuller(s, autolag="AIC")[1])
    except Exception:  # noqa: BLE001  statsmodels не должен ронять скан
        return None


def scan_pair(asset_ord: str, asset_pref: str, cfg: St4Config, days: int) -> dict:
    """Бэктест одной пары. Возвращает строку отчёта (с error, если пара не торгуется)."""
    row: dict = {"pair": f"{asset_ord}/{asset_pref}"}
    try:
        ord_code = feed.nearest_series(asset_ord, cfg.instruments.rollover_days_before_expiry)["SECID"]
        pref_code = feed.nearest_series(asset_pref, cfg.instruments.rollover_days_before_expiry)["SECID"]
        spec_ord = feed.instrument_spec(ord_code, Role.ORDINARY)
        spec_pref = feed.instrument_spec(pref_code, Role.PREFERRED)
    except Exception as e:  # noqa: BLE001
        row["error"] = f"серии не найдены: {e}"
        return row
    row["legs"] = {"ord": ord_code, "pref": pref_code,
                   "ord_expiry": spec_ord.expiry, "pref_expiry": spec_pref.expiry}

    since = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        df = feed.read_ohlcv_moex_range(cfg, since, ord_code, pref_code)
    except Exception as e:  # noqa: BLE001
        row["error"] = f"история недоступна: {e}"
        return row
    need = cfg.strategy.sma_period + 20
    if len(df) < need:
        row["error"] = f"мало данных: {len(df)} баров (нужно > {need})"
        return row

    spread = spread_series(df)
    row["bars"] = len(df)
    row["spread_mean"] = round(float(spread.mean()), 1)
    row["spread_sigma"] = round(float(spread.std(ddof=0)), 1)
    row["adf_p"] = _adf_pvalue(spread)

    res = run_backtest(df, cfg, spec_ord, spec_pref)
    for k in ("trades", "win_rate_pct", "net_pnl_rub", "fees_rub", "return_pct",
              "max_drawdown_pct", "stops", "avg_bars_held", "avg_pnl_rub"):
        row[k] = res[k]
    row["open_unrealized_rub"] = res["open_unrealized_rub"]

    # устойчивость: метрики по половинам периода (каждая со своим прогревом BB)
    half = len(df) // 2
    if half > need:
        h1 = run_backtest(df.iloc[:half], cfg, spec_ord, spec_pref)
        h2 = run_backtest(df.iloc[half:], cfg, spec_ord, spec_pref)
        row["net_h1"] = h1["net_pnl_rub"]
        row["net_h2"] = h2["net_pnl_rub"]
        row["trades_h1"] = h1["trades"]
        row["trades_h2"] = h2["trades"]
    return row


def run_scan(days: int = 60, stop_sigma: float | None = None,
             pairs: list[tuple[str, str]] | None = None,
             cfg: St4Config | None = None) -> dict:
    """Скан всех пар; результат сохраняется в OUT_JSON (его читает веб-отчёт)."""
    cfg = St4Config(**(cfg or St4Config()).model_dump())
    cfg.connector.mode = "paper"          # скан — только симуляция
    if stop_sigma is not None:
        cfg.strategy.stop_sigma = stop_sigma
    rows = [scan_pair(o, p, cfg, days) for o, p in (pairs or DEFAULT_PAIRS)]
    report = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "days": days,
        "interval_min": cfg.strategy.candle_interval_minutes,
        "sma_period": cfg.strategy.sma_period,
        "sigma_mult": cfg.strategy.sigma_multiplier,
        "stop_sigma": cfg.strategy.stop_sigma,
        "entry_trigger": cfg.strategy.entry_trigger,
        "deviation_mode": cfg.strategy.deviation_mode,
        "rows": rows,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(_clean(report), ensure_ascii=False))
    return report


def _clean(obj):
    """NaN/inf → None (JSON их не допускает)."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def _print_table(report: dict) -> None:
    hdr = ["pair", "legs", "bars", "sigma", "adf_p", "trades", "win%", "net₽",
           "ret%", "maxDD%", "net_h1", "net_h2"]
    print(f"Скан ord/pref FORTS: {report['days']}д, BB({report['sma_period']}, "
          f"{report['sigma_mult']}σ), stop_sigma={report['stop_sigma']}, "
          f"вход {report['entry_trigger']}\n")
    lines = []
    for r in report["rows"]:
        if "error" in r:
            lines.append([r["pair"], r["error"], *[""] * (len(hdr) - 2)])
            continue
        lines.append([
            r["pair"], f"{r['legs']['ord']}/{r['legs']['pref']}", r["bars"],
            r["spread_sigma"], "—" if r["adf_p"] is None else f"{r['adf_p']:.3f}",
            r["trades"], r["win_rate_pct"], r["net_pnl_rub"], r["return_pct"],
            r["max_drawdown_pct"], r.get("net_h1", ""), r.get("net_h2", ""),
        ])
    widths = [max(len(str(x)) for x in [h, *[ln[i] for ln in lines]])
              for i, h in enumerate(hdr)]
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(hdr)))
    print("-" * (sum(widths) + 2 * len(widths)))
    for ln in lines:
        print("  ".join(str(x).ljust(widths[i]) for i, x in enumerate(ln)))


def main() -> None:
    ap = argparse.ArgumentParser(description="Скан пар обычка/преф FORTS (бэктест st4)")
    ap.add_argument("--days", type=int, default=60, help="период истории, дней")
    ap.add_argument("--stop-sigma", type=float, default=None,
                    help="защитный стоп в σ (по умолчанию из конфига st4)")
    ap.add_argument("--pairs", default="",
                    help="пары ASSETCODE через запятую, формат ORD:PREF (дефолт: " +
                         ", ".join(f"{o}:{p}" for o, p in DEFAULT_PAIRS) + ")")
    ap.add_argument("--csv", default="", help="путь CSV (опционально)")
    args = ap.parse_args()

    pairs = None
    if args.pairs.strip():
        pairs = [tuple(p.split(":", 1)) for p in args.pairs.split(",") if ":" in p]

    report = run_scan(args.days, args.stop_sigma, pairs)
    _print_table(report)
    print(f"\nJSON для веб-отчёта: {OUT_JSON}")

    if args.csv:
        ok_rows = [r for r in report["rows"] if "error" not in r]
        if ok_rows:
            with Path(args.csv).open("w", newline="") as f:
                fields = sorted({k for r in ok_rows for k in r if k != "legs"})
                w = csv.DictWriter(f, fieldnames=["pair", *[x for x in fields if x != "pair"]])
                w.writeheader()
                for r in ok_rows:
                    w.writerow({k: v for k, v in r.items() if k != "legs"})
            print(f"CSV: {args.csv}")


if __name__ == "__main__":
    main()
