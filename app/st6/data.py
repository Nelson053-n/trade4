"""Данные ST6 из MOEX ISS: история вечных фьючерсов (SWAPRATE = фандинг) и ближний
квартальник для хеджа. Переиспользует REST-обвязку st4.data_feed (_get/_table/list_series).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from ..st4 import data_feed as feed

ISS = "https://iss.moex.com/iss"


def perp_history(secid: str, days: int = 30) -> list[dict]:
    """Дневная история вечного фьюча: [{date, settle, swaprate, close}, …] (борда RFUD).
    SWAPRATE — фандинг за день в ЕДИНИЦАХ ЦЕНЫ перпа (₽ = swaprate × пункт-стоимость × лоты);
    положительный фандинг платят лонги → ШОРТ его ПОЛУЧАЕТ."""
    frm = (date.today() - timedelta(days=days * 2)).isoformat()
    rows, start, out = [], 0, []
    while True:
        doc = feed._get(f"{ISS}/history/engines/futures/markets/forts/securities/{secid}.json"
                        f"?from={frm}&start={start}")
        data = feed._table(doc, "history")
        if not data:
            break
        rows += data
        start += len(data)
        if len(data) < 100:
            break
    for r in rows:
        if r.get("BOARDID") != "RFUD" or r.get("SETTLEPRICE") is None:
            continue
        out.append({"date": r["TRADEDATE"], "settle": float(r["SETTLEPRICE"]),
                    "swaprate": float(r.get("SWAPRATE") or 0.0),
                    "close": float(r.get("CLOSE") or r["SETTLEPRICE"])})
    return out[-days:]


def near_quarterly(asset: str, min_days_to_expiry: int = 0) -> tuple[str, str]:
    """(secid, expiry ISO) ближней квартальной серии актива с d2e > порога.
    Для ролла: min_days_to_expiry = roll_days_before → отдаст СЛЕДУЮЩУЮ серию."""
    s = feed.nearest_series(asset, min_days_to_expiry=min_days_to_expiry)
    return s["SECID"], s["LASTTRADEDATE"]


def quart_settle(secid: str) -> tuple[float, float]:
    """(последний settle, последний close) квартальника с борды RFUD за последние дни."""
    frm = (date.today() - timedelta(days=10)).isoformat()
    doc = feed._get(f"{ISS}/history/engines/futures/markets/forts/securities/{secid}.json"
                    f"?from={frm}")
    data = [r for r in feed._table(doc, "history")
            if r.get("BOARDID") == "RFUD" and r.get("SETTLEPRICE") is not None]
    if not data:
        raise RuntimeError(f"нет settle для {secid}")
    last = data[-1]
    return float(last["SETTLEPRICE"]), float(last.get("CLOSE") or last["SETTLEPRICE"])


def point_value(secid: str) -> float:
    """Стоимость пункта цены в ₽ (STEPPRICE/MINSTEP): IMOEXF=10, MX=1, SBERF=100, SR=1."""
    doc = feed._get(f"{ISS}/engines/futures/markets/forts/securities/{secid}.json")
    rec = {r["SECID"]: r for r in feed._table(doc, "securities")}.get(secid)
    if rec is None:
        raise RuntimeError(f"нет спецификации {secid}")
    return float(rec["STEPPRICE"]) / float(rec["MINSTEP"])


def days_to(iso: str) -> int:
    return (datetime.fromisoformat(iso).date() - date.today()).days
