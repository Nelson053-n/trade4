"""Непрерывный ряд 60м-баров из КВАРТАЛЬНЫХ контрактов FORTS (для ресерча осей ST9).

Зачем отдельно от `backtest.py`. Тот работает с перпами (USDRUBF, GLDRUBF, IMOEXF) —
у них один непрерывный SECID и склейка не нужна. Квартальники (BR, NG, ED, SBRF, GAZR…)
живут сериями по 3 месяца, и чтобы проверить их как кандидатов в корзину, ряд надо
собирать вручную.

ДВЕ ЛОВУШКИ, которые здесь решены:

1. FRONT-MONTH ПО ОБЪЁМУ, не по открытому интересу. OI требует отдельного
   пагинированного history-запроса на КАЖДУЮ серию — на 124 сериях BR это часы.
   Объём приходит вместе со свечами, а ликвидность перетекает в новую серию
   одновременно с OI, так что критерий эквивалентный.

2. RATIO BACK-ADJUST. Серии стоят по-разному (контанго/бэквордация), и наивная
   склейка close-to-close даёт ЛОЖНЫЙ ГЭП в момент ролла — трендовая система
   немедленно выдаст на нём фиктивный пробой Donchian. Здесь на каждом ролле вся
   прошлая история умножается на отношение цен двух контрактов, что сохраняет
   доходности и убирает разрыв.

Плюс сглаживание дребезга: смена лидера принимается, только если новый контракт
держит первенство 3 дня подряд — иначе на границе ролла ряд прыгал бы туда-обратно.

РЕЗУЛЬТАТ ИССЛЕДОВАНИЯ 18.08.2026 (ради которого это писалось): ни один квартальник
в корзину не годится. Независимые (BR ρ=−0.015, NG, ED) УБЫТОЧНЫ на 60м, причём у BR
отрицателен и gross (−243 828) — тренда на этом ТФ просто нет, издержки ни при чём.
Прибыльные (GAZPF net/DD 3.52) ДУБЛИРУЮТ существующие оси (ρ=+0.89 к IMOEXF):
добавление GAZPF даёт ×1.85 прибыли при ×1.83 просадки, качество портфеля +1.1%.
Подробности — память st9-full-audit-2026-08-18.
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import time as _time
import urllib.request as _u

from .engine import Bar
from .service import ISS, bar_is_closed, _now_ms_frame


def contracts_of(asset: str, days_back: int = 1530, step_days: int = 30) -> list[str]:
    """Все SECID серий актива за период. Идём срезами по history-борде: список
    торгуемых контрактов на дату.

    Шаг 30 дней (не 14): даже месячные серии, как BR, живут ≥30 дней, а квартальные
    втрое дольше — более частые срезы лишь умножают запросы. На 1530 днях шаг 14 давал
    ~107 дат × ~5 страниц = 535 запросов, то есть минуты только на поиск серий.

    ⚠️ Кидает RuntimeError, если ISS не ответил. Раньше сетевой сбой молча превращался
    в `break` и функция возвращала ПУСТОЙ список — вызывающий код принимал это за
    «серий нет» и падал дальше по стеку (инцидент 18.08: «серий найдено: 0» при том,
    что на 400 днях та же функция находила 20)."""
    today = _dt.date.today()
    out: set[str] = set()
    failed = 0
    probed = 0
    # БЫСТРЫЙ ПУТЬ: фильтр по assetcode отдаёт ТЕКУЩИЕ серии одним запросом (~1.5с
    # против минут перебора). Диапазон дат он игнорирует, поэтому исторические серии
    # всё равно ищем срезами ниже — но свежие уже есть, и если истории не нужно,
    # вызов с малым days_back отработает почти мгновенно.
    try:
        url = (f"{ISS}/history/engines/futures/markets/forts/securities.json"
               f"?iss.meta=off&assetcode={asset}&iss.only=history&start=0")
        with _u.urlopen(url, timeout=30) as r:
            h = _json.loads(r.read())["history"]
        ci = {c: i for i, c in enumerate(h["columns"])}
        for x in (h.get("data") or []):
            sec = x[ci["SECID"]]
            # отсеиваем КАЛЕНДАРНЫЕ СПРЕДЫ (BRF7BRG7) — это отдельный инструмент
            # «разница двух серий», а не контракт; в склейку он попасть не должен
            if len(sec) <= len(asset) + 2:
                out.add(sec)
    except Exception:  # noqa: BLE001  быстрый путь не критичен — ниже полный перебор
        pass
    d = today - _dt.timedelta(days=days_back)
    while d < today:
        if d.weekday() < 5:
            probed += 1
            start = 0
            while True:
                url = (f"{ISS}/history/engines/futures/markets/forts/securities.json"
                       f"?iss.meta=off&date={d.isoformat()}&iss.only=history&start={start}")
                h = None
                for a in range(3):
                    try:
                        with _u.urlopen(url, timeout=30) as r:
                            h = _json.loads(r.read())["history"]
                        break
                    except Exception:  # noqa: BLE001  сетевой сбой ISS — ретраим
                        if a == 2:
                            h = None
                            break
                        _time.sleep(1 + a)
                if not h:
                    failed += 1
                    break
                data = h.get("data") or []
                if not data:
                    break
                ci = {c: i for i, c in enumerate(h["columns"])}
                for x in data:
                    if x[ci["ASSETCODE"]] == asset:
                        out.add(x[ci["SECID"]])
                start += len(data)
                if len(data) < 100:
                    break
        d += _dt.timedelta(days=step_days)
    if not out and failed:
        raise RuntimeError(f"contracts_of({asset}): ISS не ответил на {failed} из "
                           f"{probed} дат — результат недостоверен")
    return sorted(out)


def candles(secid: str, interval: int = 60, frm: str = "2022-01-01") -> list[tuple]:
    """Свечи одной серии: (ts, o, h, l, c, value). Формирующийся бар отбрасывается
    тем же `bar_is_closed`, что в live — иначе ряд ресерча разошёлся бы с боевым."""
    out: list[tuple] = []
    start, seen = 0, set()
    now_ms = _now_ms_frame()
    while True:
        url = (f"{ISS}/engines/futures/markets/forts/securities/{secid}/candles.json"
               f"?iss.meta=off&interval={interval}&from={frm}&start={start}")
        d = None
        for a in range(4):
            try:
                with _u.urlopen(url, timeout=40) as r:
                    d = _json.loads(r.read())
                break
            except Exception:  # noqa: BLE001
                if a == 3:
                    return out
                _time.sleep(2 * (a + 1))
        rows = d["candles"]["data"]
        if not rows:
            break
        ci = {c: i for i, c in enumerate(d["candles"]["columns"])}
        for x in rows:
            ts = int(_dt.datetime.fromisoformat(x[ci["begin"]]).timestamp() * 1000)
            if ts in seen or not bar_is_closed(ts, interval, now_ms):
                continue
            seen.add(ts)
            # ОБОРОТ БЕРЁМ ИЗ `volume` (контракты), НЕ из `value` (рубли): у FORTS
            # value приходит НУЛЁМ (проверено 18.08 на BRU6: Σvalue=0 при
            # Σvolume=4 173 837). На нуле выбор front-month вырождался в случайный —
            # склейка давала 1 ролл вместо ~10 и прыжок BRM6→BRF7 через 7 серий
            # с ratio 0.868, т.е. фиктивный гэп 13% прямо в ряду.
            vol = x[ci["volume"]] if "volume" in ci else None
            if not vol:
                vol = x[ci["value"]] if "value" in ci else 0
            out.append((ts, float(x[ci["open"]]), float(x[ci["high"]]),
                        float(x[ci["low"]]), float(x[ci["close"]]), float(vol or 0)))
        start += len(rows)
    out.sort()
    return out


def splice(secids: list[str], frm: str = "2022-01-01",
           smooth_days: int = 3) -> tuple[list[Bar], list[tuple]]:
    """Склеить серии в непрерывный ряд. Возвращает (бары, роллы).

    Каждый ролл — (индекс, ratio, день, из_серии, в_серию); ratio уже применён к
    истории ДО ролла (back-adjust), т.е. бары готовы к подаче в St9Engine."""
    cand = {}
    for s in secids:
        c = candles(s, 60, frm)
        if len(c) >= 50:
            cand[s] = c
    if not cand:
        return [], []

    by_day: dict[str, dict] = {}
    for s, rows in cand.items():
        for r in rows:
            day = _dt.datetime.fromtimestamp(r[0] / 1000).date().isoformat()
            slot = by_day.setdefault(day, {}).setdefault(s, [0.0, []])
            slot[0] += r[5]
            slot[1].append(r)

    front = {day: max(m, key=lambda s: m[s][0]) for day, m in by_day.items()}
    days = sorted(front)

    # дребезг на границе ролла: принимаем смену, только если лидер держится N дней
    smooth, cur = {}, front[days[0]]
    for i, day in enumerate(days):
        c = front[day]
        if c != cur:
            run, j = 1, i + 1
            while j < len(days) and front[days[j]] == c:
                run += 1
                j += 1
            if run >= smooth_days:
                cur = c
        smooth[day] = cur

    raw, rolls, prev = [], [], None
    for day in days:
        s = smooth[day]
        rows = by_day[day].get(s)
        if not rows:
            continue
        if prev is not None and s != prev:
            old = by_day[day].get(prev)
            if old:
                p_new = sorted(rows[1])[0][4]
                p_old = sorted(old[1])[-1][4]
                if p_old > 0 and p_new > 0:
                    rolls.append((len(raw), p_new / p_old, day, prev, s))
        prev = s
        raw.extend(sorted(rows[1]))

    # back-adjust: история ДО каждого ролла масштабируется на его ratio
    factor = [1.0] * len(raw)
    for idx, ratio, _day, _a, _b in reversed(rolls):
        for k in range(idx):
            factor[k] *= ratio

    seen, out = set(), []
    for k, r in enumerate(raw):
        if r[0] in seen:
            continue
        seen.add(r[0])
        f = factor[k]
        out.append(Bar(ts=r[0], o=r[1] * f, h=r[2] * f, l=r[3] * f, c=r[4] * f))
    out.sort(key=lambda b: b.ts)
    return out, rolls
