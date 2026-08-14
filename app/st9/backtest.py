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
import json as _j
import urllib.request as _u

ISS = "https://iss.moex.com/iss"
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


def swap_rates(secid: str, frm: str = "2022-01-01", to: str = None) -> dict:
    """Фандинг перпа по дням: {"YYYY-MM-DD": swaprate}. Борда RFUD, поле SWAPRATE.

    SWAPRATE — начисление за день в ЕДИНИЦАХ ЦЕНЫ перпа (₽ = swaprate × pv × лоты).
    ЗНАК: положительный фандинг ПЛАТЯТ ЛОНГИ, шорт его ПОЛУЧАЕТ (канон st6/data.py:16).

    Зачем (12.08): ST9 торгует ВЕЧНЫМИ фьючерсами со средним удержанием 4.3 дня, а
    фандинга в модели издержек не было ВООБЩЕ — при том что st6/st7 его считают.
    Фактические ставки 2024-2026: USDRUBF +20.7%, IMOEXF +29.1%, GLDRUBF +27.6%
    ГОДОВЫХ — больше комиссии и лага вместе взятых. Игнорировать нельзя ни при
    каком знаке: ставка волатильна и в другом режиме может развернуться."""
    to = to or dt.date.today().isoformat()
    out, start = {}, 0
    while True:
        url = (f"{ISS}/history/engines/futures/markets/forts/boards/RFUD/"
               f"securities/{secid}.json?iss.meta=off&from={frm}&till={to}"
               f"&iss.only=history&start={start}")
        try:
            with _u.urlopen(url, timeout=30) as r:
                h = _j.loads(r.read())["history"]
        except Exception:
            break
        rows = h.get("data") or []
        if not rows:
            break
        ci = {c: i for i, c in enumerate(h["columns"])}
        if "SWAPRATE" in ci:
            for x in rows:
                v = x[ci["SWAPRATE"]]
                if v is not None:
                    out[x[ci["TRADEDATE"]]] = float(v)
        start += len(rows)
        if len(rows) < 100:
            break
    return out


def run(secid, don_enter, don_exit, atr_mult, bars, notional=100_000.0,
        fee_pct=0.05, slip_pct=0.0, allow_short=True, curve=None, swaps=None,
        curve_realized=None):
    """Прогон. slip_pct — проскальзывание в % на сторону (ухудшает цену исполнения).

    curve: если передать dict, в него пишется equity ПО КАЖДОМУ БАРУ
    (реализованное + плавающее) — нужно для честной mark-to-market просадки,
    см. `stats(..., curve=...)` и `portfolio_dd`.

    curve_realized: если передать dict — в него пишется ТОЛЬКО реализованная часть
    (без плавающей). Пара curve/curve_realized нужна для сравнения метрик стопа
    просадки: guard считает по mark-to-market, из-за чего откат бумажной прибыли
    трендовой позиции выглядит как потеря капитала (разбор 15.08).

    swaps: карта фандинга из swap_rates(secid). Начисляется за каждый календарный
    день удержания: лонг платит положительный фандинг, шорт получает. Без неё
    результат ЗАВЫШАЕТ издержки лонгов и ЗАНИЖАЕТ доход шортов — на текущей
    истории портфель без фандинга недосчитывает ~+10 466₽ на 100к/ось."""
    pv = PV[secid]
    eng = St9Engine(secid=secid, don_enter=don_enter, don_exit=don_exit,
                    atr_mult=atr_mult, atr_period=14, pv=pv,
                    fee_pct_notional=fee_pct, allow_short=allow_short)
    trades = []
    realized = 0.0
    funding = 0.0          # накопленный фандинг открытой позиции (₽)
    prev_day = None
    for b in bars:
        lots = max(1, int(notional / (b.c * pv)))
        # ── ФАНДИНГ: начисляется РАЗ В КАЛЕНДАРНЫЙ ДЕНЬ, пока позиция открыта.
        # Внутри дня баров много — начисляем на первом баре нового дня, иначе
        # переплатили бы в 10-14 раз (по числу торговых часов).
        if swaps and eng.position is not None:
            day = dt.datetime.fromtimestamp(b.ts / 1000).date().isoformat()
            if prev_day is not None and day != prev_day:
                sr = swaps.get(day)
                if sr is not None:
                    p = eng.position
                    # лонг ПЛАТИТ положительный фандинг, шорт ПОЛУЧАЕТ
                    f = -sr * pv * p.lots if p.side == "long" else sr * pv * p.lots
                    funding += f
                    p.fees_rub -= f   # входит в net сделки через engine.close()
            prev_day = day
        elif swaps:
            prev_day = dt.datetime.fromtimestamp(b.ts / 1000).date().isoformat()
        sig = eng.step(b, lots)
        if sig:
            px = sig["px"]
            if sig["act"] in ("close", "reverse"):
                # проскальзывание на выходе: исполняемся хуже цены бара
                d = 1 if eng.position.side == "long" else -1
                px_exec = px * (1 - slip_pct / 100 * d)
                tr = eng.close(px_exec, b.ts, sig["reason"])
                trades.append(tr)
                realized += tr.net_pnl_rub
            if sig["act"] in ("open", "reverse"):
                side = sig["new_side"]
                d = 1 if side == "long" else -1
                px_exec = px * (1 + slip_pct / 100 * d)
                eng.open(side, px_exec, lots, b.ts, sig["atr"])
        if curve is not None:
            unreal = eng.unrealized_rub(b.c) if eng.position else 0.0
            curve[b.ts] = realized + unreal
        if curve_realized is not None:
            curve_realized[b.ts] = realized
    return trades


def _dd_from_curve(points) -> float:
    """Максимальная просадка по последовательности значений equity."""
    peak = 0.0
    dd = 0.0
    for eq in points:
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return dd


def portfolio_dd(curves: dict) -> float:
    """Mark-to-market просадка ПОРТФЕЛЯ из нескольких осей.

    Складывать `dd` отдельных осей нельзя — их просадки не совпадают по времени.
    Строим общую кривую по объединённой шкале баров, каждая ось держит последнее
    известное значение (оси торгуются на разных инструментах, бары не выровнены).

    curves: {secid: {ts: equity}} — заполняются через run(..., curve=d)."""
    if not curves:
        return 0.0
    ts_all = sorted({t for c in curves.values() for t in c})
    last = {k: 0.0 for k in curves}
    total = []
    for ts in ts_all:
        for k, c in curves.items():
            if ts in c:
                last[k] = c[ts]
        total.append(sum(last.values()))
    return round(_dd_from_curve(total))


def stats(trades, curve=None):
    """Метрики прогона.

    ⚠️ dd: если передан `curve` (из run(..., curve=d)) — просадка MARK-TO-MARKET, то
    есть с плавающим убытком открытой позиции. Именно её видит оператор на счёте и
    именно она грозит маржин-коллом. Без curve считается просадка ПО ЗАКРЫТЫМ СДЕЛКАМ
    — она СИСТЕМАТИЧЕСКИ ЗАНИЖЕНА (аудит 12.08: в 1.15-1.19× по осям st9), потому что
    убыток внутри открытой позиции в неё не попадает. Для решений о размере позиции
    брать только mark-to-market."""
    if not trades:
        return dict(n=0, net=0, gross=0, fees=0, pf=0.0, win="0/0", dd=0, dd_mtm=None)
    net = sum(t.net_pnl_rub for t in trades)
    gross = sum(t.gross_pnl_rub for t in trades)
    fees = sum(t.fees_rub for t in trades)
    wins = sum(1 for t in trades if t.net_pnl_rub > 0)
    up = sum(t.net_pnl_rub for t in trades if t.net_pnl_rub > 0)
    dn = -sum(t.net_pnl_rub for t in trades if t.net_pnl_rub < 0)
    pf = up / dn if dn else float("inf")
    eq = 0.0
    closed = []
    for t in trades:
        eq += t.net_pnl_rub
        closed.append(eq)
    dd_closed = _dd_from_curve(closed)
    dd_mtm = _dd_from_curve([curve[k] for k in sorted(curve)]) if curve else None
    return dict(n=len(trades), net=round(net), gross=round(gross), fees=round(fees),
                pf=round(pf, 2), win=f"{wins}/{len(trades)}",
                dd=round(dd_mtm if dd_mtm is not None else dd_closed),
                dd_closed=round(dd_closed),
                dd_mtm=round(dd_mtm) if dd_mtm is not None else None)
