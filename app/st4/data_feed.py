"""Источник данных st4 — MOEX ISS (публичный REST, без ключей).

ISS отдаёт свечи фьючерсов FORTS (engine=futures, market=forts) с задержкой, без
авторизации. Реализованы: справочник серий + авто-роллировер ближайшей ликвидной,
спецификация инструмента (тик/шаг/лот), свечи 10m обеих ног с inner-join по времени,
а также синтетический генератор спреда SBPR−SBRF для офлайн-демо и тестов.

ВНИМАНИЕ: 5-минутных свечей в ISS нет (интервалы 1/10/60/24/7/31). Дефолт st4 — 10m.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from .config import St4Config
from .models import InstrumentSpec, Role

ISS = "https://iss.moex.com/iss"
# ISS отдаёт время свечей (begin) в МОСКОВСКОМ времени сессии FORTS (UTC+3, без перехода
# на летнее/зимнее — РФ его не использует). Метим именно так, иначе ts уезжает на 3 часа.
_MSK = timezone(timedelta(hours=3))
# минуты ТЗ → код интервала свечей ISS
_INTERVAL = {1: 1, 10: 10, 60: 60}
_HTTP_TIMEOUT = 30


def _get(url: str) -> dict:
    """GET JSON с ISS. Заголовок UA — ISS иногда режет дефолтный python-urllib."""
    req = urllib.request.Request(url, headers={"User-Agent": "pairsignal-st4/1.0"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:  # noqa: S310 (доверенный хост)
        return json.loads(r.read().decode("utf-8"))


def _table(doc: dict, name: str) -> list[dict]:
    """ISS-блок {columns,data} → список dict'ов по именам колонок."""
    blk = doc[name]
    cols = blk["columns"]
    return [dict(zip(cols, row)) for row in blk["data"]]


def list_series(asset: str) -> list[dict]:
    """Серии фьючерса по базовому активу (SBRF/SBPR), отсортированы по дате экспирации.

    URL-фильтр assetcode на этом эндпоинте ISS не работает — фильтруем в коде по ASSETCODE.
    """
    url = (f"{ISS}/engines/futures/markets/forts/securities.json?iss.meta=off"
           "&securities.columns=SECID,SHORTNAME,LASTTRADEDATE,ASSETCODE")
    rows = [r for r in _table(_get(url), "securities") if r["ASSETCODE"] == asset]
    rows.sort(key=lambda r: r["LASTTRADEDATE"])
    return rows


def nearest_series(asset: str, min_days_to_expiry: int = 0) -> dict:
    """Ближайшая серия, до экспирации которой ещё ≥ min_days_to_expiry дней.

    Реализует выбор контракта для авто-роллировера (§6.4): не берём серию,
    которая истекает в окне роллировера — переключаемся на следующую.
    """
    today = datetime.now(timezone.utc).date()
    for r in list_series(asset):
        exp = datetime.strptime(r["LASTTRADEDATE"], "%Y-%m-%d").date()
        if (exp - today).days >= min_days_to_expiry:
            return r
    raise RuntimeError(f"нет ликвидной серии {asset} (мин. {min_days_to_expiry} дн до экспирации)")


def instrument_spec(secid: str, role: Role) -> InstrumentSpec:
    """Спецификация серии: MINSTEP (тик), STEPPRICE (₽/шаг), LOTSIZE, LASTTRADEDATE."""
    url = (f"{ISS}/engines/futures/markets/forts/securities/{secid}.json?iss.meta=off"
           "&iss.only=securities"
           "&securities.columns=SECID,MINSTEP,STEPPRICE,LOTVOLUME,LASTTRADEDATE")
    rows = _table(_get(url), "securities")
    if not rows:
        raise RuntimeError(f"нет спецификации инструмента {secid}")
    r = rows[0]
    return InstrumentSpec(
        code=secid, role=role,
        tick_size=float(r.get("MINSTEP") or 1.0),
        tick_value_rub=float(r.get("STEPPRICE") or 1.0),
        lot=int(r.get("LOTVOLUME") or 1),   # FORTS: размер лота в колонке LOTVOLUME
        expiry=r.get("LASTTRADEDATE"),
    )


def leg_margin(secid: str) -> float:
    """Гарантийное обеспечение (INITIALMARGIN, ₽) одного контракта серии — из ISS.

    ГО меняется ежедневно (биржа пересчитывает по волатильности) — берём актуальное.
    """
    url = (f"{ISS}/engines/futures/markets/forts/securities/{secid}.json?iss.meta=off"
           "&iss.only=securities&securities.columns=SECID,INITIALMARGIN")
    rows = _table(_get(url), "securities")
    if not rows or rows[0].get("INITIALMARGIN") is None:
        return 0.0
    return float(rows[0]["INITIALMARGIN"])


def resolve_legs(cfg: St4Config) -> tuple[InstrumentSpec, InstrumentSpec]:
    """Определить коды и спецификации обеих ног (с учётом роллировера).

    Если коды заданы явно и auto_rollover=False — берём их. Иначе подбираем ближайшую
    ликвидную серию каждого актива, не входящую в окно роллировера.
    """
    inst = cfg.instruments
    if not inst.auto_rollover and inst.leg_ordinary_code and inst.leg_preferred_code:
        ord_code, pref_code = inst.leg_ordinary_code, inst.leg_preferred_code
    else:
        days = inst.rollover_days_before_expiry
        ord_code = nearest_series(inst.asset_ordinary, days)["SECID"]
        pref_code = nearest_series(inst.asset_preferred, days)["SECID"]
    return (instrument_spec(ord_code, Role.ORDINARY),
            instrument_spec(pref_code, Role.PREFERRED))


def _candles_ohlcv(secid: str, interval: int, since: datetime | None = None,
                   count: int | None = None) -> pd.DataFrame:
    """Close+volume свечей секции по begin-времени (UTC ms) → DataFrame[close, volume].

    ISS отдаёт свечи страницами по ~500, ВСЕГДА от старых к новым через start=. Чтобы
    получить последние `count` баров, листаем все страницы до конца (start += page) и
    берём хвост — без `from` первая страница это древнейшие свечи серии, не свежие.
    Если задан `since` — стартуем с этой даты (бэктест за период), листаем до конца.
    begin в ответе — московское время сессии FORTS; приводим к UTC ms единообразно
    (для синхронизации ног важна согласованность ключа, а не абсолютная TZ).
    """
    icode = _INTERVAL.get(interval, 10)
    # since выбираем с запасом, чтобы покрыть count баров (10m: count·интервал + буфер)
    if since is None and count is not None:
        from datetime import timedelta
        span_min = count * interval * 1.6 + 3 * 24 * 60   # ×1.6 на простои + 3 дня выходных
        since = datetime.now(timezone.utc) - timedelta(minutes=span_min)
    frm = f"&from={since.strftime('%Y-%m-%d')}" if since else ""
    closes: dict[int, float] = {}
    vols: dict[int, float] = {}
    start = 0
    while True:
        url = (f"{ISS}/engines/futures/markets/forts/securities/{secid}/candles.json"
               f"?iss.meta=off&interval={icode}{frm}&start={start}"
               "&candles.columns=close,volume,begin")
        rows = _table(_get(url), "candles")
        if not rows:
            break
        for r in rows:
            # begin — московское время сессии FORTS; метим как MSK → корректный UTC unix ms
            ts = int(datetime.strptime(r["begin"], "%Y-%m-%d %H:%M:%S")
                     .replace(tzinfo=_MSK).timestamp() * 1000)
            closes[ts] = float(r["close"])
            vols[ts] = float(r.get("volume") or 0.0)
        start += len(rows)
        if len(rows) < 500:  # последняя страница (ISS отдаёт по 500)
            break
    df = pd.DataFrame({"close": pd.Series(closes, dtype="float64"),
                       "volume": pd.Series(vols, dtype="float64")}).sort_index()
    df.index.name = "ts"
    if count is not None:
        df = df.iloc[-count:]
    return df


def _candles(secid: str, interval: int, since: datetime | None = None,
             count: int | None = None) -> pd.Series:
    """Close-серия свечей секции (тонкая обёртка над _candles_ohlcv)."""
    return _candles_ohlcv(secid, interval, since=since, count=count)["close"]


def read_ohlcv_moex(cfg: St4Config, limit: int = 600,
                    ord_code: str | None = None, pref_code: str | None = None) -> pd.DataFrame:
    """Close+volume обеих ног (SBRF, SBPR) за последние ~limit баров, inner-join по времени.

    price_a = SBRF (обыкновенные), price_b = SBPR (привилегированные); vol_a/vol_b — объёмы
    ног (для объёмного фильтра входа). Спред в индикаторах считается как close_pref − close_ord
    (= price_b − price_a), знак по ТЗ §2. Коды серий можно передать явно, иначе из resolve_legs.
    """
    if ord_code is None or pref_code is None:
        spec_ord, spec_pref = resolve_legs(cfg)
        ord_code, pref_code = spec_ord.code, spec_pref.code
    iv = cfg.strategy.candle_interval_minutes
    a = _candles_ohlcv(ord_code, iv, count=limit)    # SBRF
    b = _candles_ohlcv(pref_code, iv, count=limit)   # SBPR
    df = pd.DataFrame({"price_a": a["close"], "price_b": b["close"],
                       "vol_a": a["volume"], "vol_b": b["volume"]}).dropna().sort_index()
    return df


def read_ohlcv_tbank(cfg: St4Config, limit: int, uid_ord: str, uid_pref: str) -> pd.DataFrame:
    """Close обеих ног из T-Bank (REAL-TIME, без лага ISS) — для sandbox-live.

    uid_ord/uid_pref — instrument uid из справочника T-Bank (резолвит вызывающий).
    Тянем закрытые свечи за окно, покрывающее limit баров; inner-join по ts.
    price_a = SBRF, price_b = SBPR (как в read_ohlcv_moex — единый контракт для движка).
    """
    from . import tbank_sandbox as _sb
    iv = cfg.strategy.candle_interval_minutes
    now = datetime.now(timezone.utc)
    # T-Bank ОГРАНИЧИВАЕТ диапазон GetCandles по интервалу (для 10m ~7 дней, для 1m ~1 день).
    # Берём окно с запасом на простои/выходные, но НЕ больше лимита (иначе HTTP 400 "max period").
    _MAX_DAYS = {1: 1, 5: 7, 10: 7, 60: 90}
    want_min = limit * iv * 2 + 3 * 24 * 60          # нужное окно (×2 + 3 дня на выходные)
    cap_min = _MAX_DAYS.get(iv, 7) * 24 * 60          # лимит T-Bank по интервалу
    span_min = min(want_min, cap_min)
    frm = (now - timedelta(minutes=span_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
    to = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    a = pd.Series(dict(_sb.get_candles(uid_ord, iv, frm, to)), dtype="float64").sort_index()
    b = pd.Series(dict(_sb.get_candles(uid_pref, iv, frm, to)), dtype="float64").sort_index()
    a.index.name = b.index.name = "ts"
    # T-Bank get_candles отдаёт только close (без объёма) → vol_a/vol_b=0: объёмный фильтр
    # на T-Bank-данных не срабатывает (по дизайну — фильтруем только при реальном объёме).
    df = pd.DataFrame({"price_a": a, "price_b": b, "vol_a": 0.0, "vol_b": 0.0}) \
        .dropna(subset=["price_a", "price_b"]).sort_index()
    return df.iloc[-limit:] if len(df) > limit else df


def read_ohlcv_moex_range(cfg: St4Config, since: datetime,
                          ord_code: str | None = None, pref_code: str | None = None) -> pd.DataFrame:
    """Как read_ohlcv_moex, но за период [since; now) — для бэктеста на истории."""
    if ord_code is None or pref_code is None:
        spec_ord, spec_pref = resolve_legs(cfg)
        ord_code, pref_code = spec_ord.code, spec_pref.code
    iv = cfg.strategy.candle_interval_minutes
    a = _candles_ohlcv(ord_code, iv, since=since)
    b = _candles_ohlcv(pref_code, iv, since=since)
    df = pd.DataFrame({"price_a": a["close"], "price_b": b["close"],
                       "vol_a": a["volume"], "vol_b": b["volume"]}).dropna().sort_index()
    return df


def generate_synthetic(n: int = 1500, seed: int = 23, interval_min: int = 10) -> pd.DataFrame:
    """Синтетика спреда SBPR−SBRF: mean-reverting (OU) для офлайн-демо и тестов.

    Обе ноги двигает общий рыночный фактор (как реальные SR/SP — почти равны, ~32000
    пунктов), поверх — рассогласование преф/обычка по OU-процессу вокруг небольшого
    среднего (~+80 пунктов: преф чуть дороже на текущих сериях). Амплитуда подобрана
    так, чтобы спред регулярно пробивал ±2σ — иначе сделок нет, логику не обкатать.
    """
    rng = np.random.default_rng(seed)
    base = 32000.0 * np.exp(np.cumsum(rng.normal(0, 0.0015, n)))   # общий уровень обеих ног

    # спред SBPR−SBRF по OU вокруг mu (пункты)
    spread = np.zeros(n)
    mu, theta, sigma = 80.0, 0.03, 14.0
    spread[0] = mu
    for i in range(1, n):
        spread[i] = spread[i - 1] + theta * (mu - spread[i - 1]) + rng.normal(0, sigma)

    price_ord = base - spread / 2.0      # SBRF
    price_pref = base + spread / 2.0     # SBPR
    # округляем к шагу цены (MINSTEP=1) — как реальные котировки FORTS
    price_ord = np.round(price_ord)
    price_pref = np.round(price_pref)
    # синтетический объём ног (пуассон вокруг среднего) — чтобы объёмный фильтр было на чём
    # обкатать офлайн. Реальной информации не несёт, как и сам синтетический спред.
    vol_ord = rng.poisson(1000, n).astype("float64")
    vol_pref = rng.poisson(600, n).astype("float64")
    step = interval_min * 60_000
    ts = (np.arange(n) * step + 1_700_000_000_000).astype("int64")
    df = pd.DataFrame({"price_a": price_ord, "price_b": price_pref,
                       "vol_a": vol_ord, "vol_b": vol_pref}, index=ts)
    df.index.name = "ts"
    return df


def synthetic_spec(role: Role) -> InstrumentSpec:
    """Спецификация-заглушка для офлайн-режима (совпадает с реальными SR*/SP*)."""
    code = "SRSYN" if role == Role.ORDINARY else "SPSYN"
    return InstrumentSpec(code=code, role=role, tick_size=1.0, tick_value_rub=1.0,
                          lot=100, expiry=None)
