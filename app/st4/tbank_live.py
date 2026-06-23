"""БОЕВОЙ контур T-Bank Invest API (реальные деньги).

Зеркало tbank_sandbox.py, но ходит в БОЕВЫЕ сервисы:
  • OrdersService.PostOrder / CancelOrder  — реальные ордера
  • OperationsService.GetPortfolio / GetPositions — реальные баланс/позиции
  • UsersService.GetAccounts — реальные счета

REST-слой (хост, токен, SSL, ретраи, парсинг котировок, справочник инструментов)
переиспользуется из tbank_sandbox — он общий для песочницы и боя, разделение только
по namespace метода. Здесь НЕ открываем и НЕ пополняем счёт: реальный счёт уже
существует, деньги вносятся вручную через ЛК.

⚠️ post_order здесь тратит РЕАЛЬНЫЕ деньги. Вызывается только при тройном условии
(mode==tbank_real И real_trading_armed И trading_enabled) — гейт в движке/executor.
"""
from __future__ import annotations

import hashlib

# Переиспользуем общий REST-слой и справочник инструментов из песочницы.
from .tbank_sandbox import (  # noqa: F401  (re-export для удобства)
    TBankError,
    _call,
    _q,
    _q_to_float,
    _uid,
    find_future,
    is_tradable,
    last_price,
)

_ORDERS = "tinkoff.public.invest.api.contract.v1.OrdersService"
_OPERATIONS = "tinkoff.public.invest.api.contract.v1.OperationsService"
_USERS = "tinkoff.public.invest.api.contract.v1.UsersService"


# ---------------------------------------------------------------- счета (read)
def list_accounts() -> list[dict]:
    """Реальные счета пользователя (UsersService.GetAccounts)."""
    return _call(_USERS, "GetAccounts", {}).get("accounts", [])


def account_is_open(account_id: str) -> bool:
    """Проверить, что реальный счёт существует и открыт (защита перед боевым режимом)."""
    for a in list_accounts():
        if a.get("id") == account_id and a.get("status") == "ACCOUNT_STATUS_OPEN":
            return True
    return False


# ---------------------------------------------------------------- портфель (read)
def portfolio(account_id: str) -> dict:
    """Реальный портфель (OperationsService.GetPortfolio)."""
    return _call(_OPERATIONS, "GetPortfolio", {"accountId": account_id})


def positions(account_id: str) -> dict:
    """Реальные позиции (OperationsService.GetPositions)."""
    return _call(_OPERATIONS, "GetPositions", {"accountId": account_id})


# ---------------------------------------------------------------- ордера (БОЕВЫЕ!)
def make_order_id(account_id: str, instrument_uid: str, lots: int,
                  direction: str, ts: float) -> str:
    """Детерминированный идемпотентный orderId: повтор при сетевом обрыве не задвоит ордер.

    T-Bank дедуплицирует по orderId — одинаковый id для одного логического ордера
    означает, что ретрай не создаст вторую сделку."""
    raw = f"{account_id}|{instrument_uid}|{int(lots)}|{direction}|{int(ts)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def post_order(account_id: str, instrument_uid: str, lots: int, direction: str,
               order_id: str, order_type: str = "ORDER_TYPE_MARKET",
               price: dict | None = None) -> dict:
    """⚠️ БОЕВОЙ ордер (OrdersService.PostOrder) — реальные деньги.
    direction: ORDER_DIRECTION_BUY|SELL. order_id должен быть идемпотентным (make_order_id)."""
    body = {
        "accountId": account_id,
        "instrumentId": instrument_uid,
        "quantity": str(int(lots)),
        "direction": direction,
        "orderType": order_type,
        "orderId": order_id,
    }
    if price is not None:
        body["price"] = price
    return _call(_ORDERS, "PostOrder", body)


def cancel_order(account_id: str, order_id: str) -> dict:
    """Отменить боевой ордер (OrdersService.CancelOrder) — для unwind при частичном входе."""
    return _call(_ORDERS, "CancelOrder", {"accountId": account_id, "orderId": order_id})
