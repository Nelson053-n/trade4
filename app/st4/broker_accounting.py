"""Чтение исполнений и полной истории операций без торговых действий."""
from __future__ import annotations

from .tbank_sandbox import _q_to_float


def order_fill(response: dict, unit_value: float) -> dict:
    """Цена PostOrder — рубли за инструмент; unit_value переводит её в котировку.

    Неизвестные цена/комиссия остаются None, а не превращаются в модельный факт.
    """
    lots = response.get("lotsExecuted", response.get("executedLots"))
    lots = int(lots) if lots is not None else None
    price = response.get("executedOrderPrice")
    fee = response.get("executedCommission")
    return {"lots": lots,
            "price": _q_to_float(price) / unit_value if price and unit_value > 0 else None,
            "fee": abs(_q_to_float(fee)) if fee is not None else None,
            "order_id": response.get("orderId")}


def all_operations(call, namespace: str, method: str, account_id: str,
                   frm_iso: str, to_iso: str, **kwargs) -> list[dict]:
    """Все страницы операций; повтор курсора — ошибка полноты, а не тихий обрыв."""
    params = {"accountId": account_id, "from": frm_iso, "to": to_iso,
              "state": "OPERATION_STATE_EXECUTED", "limit": 1000}
    rows: dict[str, dict] = {}
    cursors = set()
    while True:
        response = call(namespace, method, params.copy(), **kwargs)
        for item in response.get("items", []):
            # В cursor API тип называется type; сохраняем совместимость с GetOperations.
            item = dict(item)
            item["operationType"] = item.get("type", item.get("operationType", ""))
            key = item.get("id")
            if not key:
                raise ValueError("операция без id: полнота сверки не подтверждена")
            rows[key] = item
        if not response.get("hasNext"):
            return sorted(rows.values(), key=lambda row: row.get("date", ""))
        cursor = response.get("nextCursor")
        if not cursor or cursor in cursors:
            raise ValueError("курсор операций не продвигается: история неполна")
        cursors.add(cursor)
        params["cursor"] = cursor
