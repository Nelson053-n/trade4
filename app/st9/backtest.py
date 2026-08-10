"""Бэктест ST9 на ЧЕСТНОЙ модели издержек.

ЗАЧЕМ В РЕПОЗИТОРИИ (аудит 10.08): свипы 25.07 и 30.07, на которых выбраны текущие
параметры осей, гонялись эфемерными скриптами в scratchpad и УТЕРЯНЫ — ни один прогон
воспроизвести было нельзя, `backtests/JOURNAL.md` пуст. Стенд живёт здесь именно чтобы
это не повторилось.

Логика сигналов берётся из БОЕВОГО движка (`St9Engine`), не переписывается: иначе
бэктест проверяет не то, что торгует прод.

МОДЕЛЬ ИЗДЕРЖЕК — две статьи, обе обязательны:
1. Комиссия 0.05% нотионала за сторону (замер по счёту 30.07, 292 сделки). Считает сам
   движок через `_fee`, поэтому расхождение с live невозможно по построению.
2. ЛАГ ИСПОЛНЕНИЯ `slip_pct` — сигнал рождается на закрытии бара, ордер уходит следующим
   тиком опроса. Медиана замеров ~0.11%/сторону (`config.py:117-122`). Прежние свипы эту
   статью НЕ моделировали (только «2 тика» ≈0.002-0.044%), из-за чего занижали издержки
   в 1.7-3.1× — при её включении net падает вдвое-втрое, а оптимум смещается в сторону
   более редких сделок. Гонять БЕЗ неё нельзя: это ровно та ошибка, что уже дважды
   переворачивала выбор параметров (см. память st9-real-commission-005pct).

Пример:
    from app.st9.backtest import load_bars, run, stats
    bars = load_bars("USDRUBF", 1200)
    print(stats(run("USDRUBF", 70, 10, 5.0, bars, slip_pct=0.11)))
"""
import sys, datetime as dt
sys.path.insert(0, "/home/nel/trade4")

from app.st9.engine import St9Engine, Bar
from app.st9.service import iss_candles

# pv (STEPPRICE/MINSTEP) — как в проде
PV = {"USDRUBF": 1000.0, "GLDRUBF": 1.0, "IMOEXF": 10.0}


def load_bars(secid: str, days: int, interval: int = 60) -> list[Bar]:
    """Полный OHLC с ISS с ПАГИНАЦИЕЙ (ISS отдаёт максимум 500 свечей за запрос).
    Формат бара и отбрасывание формирующегося — как в live (iss_candles)."""
    import urllib.request as _u, json as _j
    from app.st9.service import ISS, bar_is_closed, _now_ms_frame
    iss_iv = 24 if interval >= 1440 else interval
    frm = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    now_ms = _now_ms_frame()
    out, start, seen = [], 0, set()
    while True:
        url = (f"{ISS}/engines/futures/markets/forts/securities/{secid}/candles.json"
               f"?iss.meta=off&interval={iss_iv}&from={frm}&start={start}")
        with _u.urlopen(url, timeout=30) as r:
            d = _j.loads(r.read())
        rows = d["candles"]["data"]
        if not rows:
            break
        ci = {c: i for i, c in enumerate(d["candles"]["columns"])}
        for x in rows:
            ts = int(dt.datetime.fromisoformat(x[ci["begin"]]).timestamp() * 1000)
            if ts in seen or not bar_is_closed(ts, interval, now_ms):
                continue
            seen.add(ts)
            out.append(Bar(ts=ts, o=float(x[ci["open"]]), h=float(x[ci["high"]]),
                           l=float(x[ci["low"]]), c=float(x[ci["close"]])))
        start += len(rows)
    out.sort(key=lambda b: b.ts)
    return out


def run(secid, don_enter, don_exit, atr_mult, bars, notional=100_000.0,
        fee_pct=0.05, slip_pct=0.0, allow_short=True):
    """Прогон. slip_pct — проскальзывание в % на сторону (ухудшает цену исполнения)."""
    pv = PV[secid]
    eng = St9Engine(secid=secid, don_enter=don_enter, don_exit=don_exit,
                    atr_mult=atr_mult, atr_period=14, pv=pv,
                    fee_pct_notional=fee_pct, allow_short=allow_short)
    trades = []
    for b in bars:
        lots = max(1, int(notional / (b.c * pv)))
        sig = eng.step(b, lots)
        if not sig:
            continue
        px = sig["px"]
        if sig["act"] in ("close", "reverse"):
            # проскальзывание на выходе: исполняемся хуже цены бара
            d = 1 if eng.position.side == "long" else -1
            px_exec = px * (1 - slip_pct / 100 * d)
            trades.append(eng.close(px_exec, b.ts, sig["reason"]))
        if sig["act"] in ("open", "reverse"):
            side = sig["new_side"]
            d = 1 if side == "long" else -1
            px_exec = px * (1 + slip_pct / 100 * d)
            eng.open(side, px_exec, lots, b.ts, sig["atr"])
    return trades


def stats(trades):
    if not trades:
        return dict(n=0, net=0, gross=0, fees=0, pf=0.0, win="0/0", dd=0)
    net = sum(t.net_pnl_rub for t in trades)
    gross = sum(t.gross_pnl_rub for t in trades)
    fees = sum(t.fees_rub for t in trades)
    wins = sum(1 for t in trades if t.net_pnl_rub > 0)
    up = sum(t.net_pnl_rub for t in trades if t.net_pnl_rub > 0)
    dn = -sum(t.net_pnl_rub for t in trades if t.net_pnl_rub < 0)
    pf = up / dn if dn else float("inf")
    eq = 0.0
    peak = 0.0
    dd = 0.0
    for t in trades:
        eq += t.net_pnl_rub
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return dict(n=len(trades), net=round(net), gross=round(gross), fees=round(fees),
                pf=round(pf, 2), win=f"{wins}/{len(trades)}", dd=round(dd))
