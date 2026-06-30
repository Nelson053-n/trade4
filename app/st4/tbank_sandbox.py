"""Песочница T-Bank Invest API для st4 — полный прогон входа/выхода пары SRM6/SPM6.

ВНИМАНИЕ / БЕЗОПАСНОСТЬ:
  • Скрипт ходит ИСКЛЮЧИТЕЛЬНО в SandboxService (виртуальные деньги, виртуальный счёт).
    Боевые методы (OrdersService.PostOrder и пр.) здесь не реализованы — отправить реальный
    ордер этим кодом технически нельзя. Это первый шаг Phase 2 из ТЗ (§14.3, бумажная торговля
    через песочницу) в рамках жёстких ограничений проекта: никакого реального исполнения.
  • Токен — только из переменной окружения TBANK_TOKEN (в .env, который в .gitignore).
    Секрет не коммитим и не печатаем.

Что делает (по шагам):
  1. читает справочник фьючерсов, находит SRM6 (SBRF) и SPM6 (SBPR), проверяет флаги торгуемости;
  2. открывает sandbox-счёт и пополняет рублями (под гарантийное обеспечение);
  3. ставит ПАРНЫЙ рыночный ордер: шорт спреда = BUY SBRF + SELL SBPR (как в st4/engine);
  4. читает портфель/позиции, показывает фактический филл;
  5. закрывает обе ноги обратным ордером, выводит итог.

Запуск:
  TBANK_TOKEN=... python -m app.st4.tbank_sandbox            # полный прогон
  TBANK_TOKEN=... python -m app.st4.tbank_sandbox --check    # только проверка инструментов
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# Песочница и боевой контур делят один хост; РАЗДЕЛЕНИЕ — по namespace метода:
# SandboxService.* работает только с виртуальным счётом. Боевые сервисы намеренно не вызываем.
_HOST = "https://invest-public-api.tbank.ru/rest"

# T-Bank подписан УЦ Минцифры РФ (Russian Trusted Root/Sub CA), которого нет в стандартном
# наборе CA Python/системы → SSL: CERTIFICATE_VERIFY_FAILED. Доверяем точечно бандлу
# tbank_ca.pem (полная цепочка до корня Минцифры). Переопределить путь — TBANK_CA_BUNDLE.
# НЕ отключаем верификацию глобально — проверка сертификата остаётся включённой.
_CA_BUNDLE = os.environ.get("TBANK_CA_BUNDLE") or str(Path(__file__).with_name("tbank_ca.pem"))


def _ssl_ctx() -> ssl.SSLContext:
    if Path(_CA_BUNDLE).exists():
        return ssl.create_default_context(cafile=_CA_BUNDLE)
    # бандла нет — обычная проверка системными CA (упадёт с понятной SSL-ошибкой)
    return ssl.create_default_context()
_SANDBOX = "tinkoff.public.invest.api.contract.v1.SandboxService"
_INSTRUMENTS = "tinkoff.public.invest.api.contract.v1.InstrumentsService"
_MARKETDATA = "tinkoff.public.invest.api.contract.v1.MarketDataService"

# серии st4 (совпадают с кодами FORTS / тикерами T-Bank)
TICKER_ORD = "SRM6"   # SBRF (обыкновенные)
TICKER_PREF = "SPM6"  # SBPR (привилегированные)


class TBankError(RuntimeError):
    pass


# Файл-хранилище токена (переживает рестарт). В .gitignore, права 0600 — секрет не в git.
_TOKEN_FILE = Path(__file__).with_name(".tbank_token")


def save_token(token: str) -> None:
    """Сохранить токен в файл (0600) + в окружение процесса. Пусто → удалить файл."""
    token = (token or "").strip()
    if token:
        os.environ["TBANK_TOKEN"] = token
        try:
            _TOKEN_FILE.write_text(token)
            _TOKEN_FILE.chmod(0o600)
        except Exception:  # noqa: BLE001  не удалось записать — токен хотя бы в env
            pass
    else:
        os.environ.pop("TBANK_TOKEN", None)
        _TOKEN_FILE.unlink(missing_ok=True)


def load_token() -> bool:
    """Подтянуть токен из файла в окружение при старте. True — если токен есть."""
    if os.environ.get("TBANK_TOKEN", "").strip():
        return True
    try:
        if _TOKEN_FILE.exists():
            tok = _TOKEN_FILE.read_text().strip()
            if tok:
                os.environ["TBANK_TOKEN"] = tok
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def has_token() -> bool:
    """Есть ли токен (в env или файле) — без раскрытия самого секрета."""
    return bool(os.environ.get("TBANK_TOKEN", "").strip()) or _TOKEN_FILE.exists()


def _token() -> str:
    tok = os.environ.get("TBANK_TOKEN", "").strip()
    if not tok:
        load_token()                       # попробовать подтянуть из файла
        tok = os.environ.get("TBANK_TOKEN", "").strip()
    if not tok:
        raise TBankError("нет токена: задайте TBANK_TOKEN в окружении (.env) или через UI")
    return tok


def _call(service: str, method: str, body: dict, _retries: int = 3) -> dict:
    """POST к REST-gateway T-Bank. Возвращает JSON-ответ. Токен из env, не печатается.

    Ретраи на транзиентных сетевых обрывах (IncompleteRead/timeout) — крупные ответы вроде
    Futures (~5МБ справочник) иногда обрываются до конца. Также ретраим HTTP 429 (rate limit)
    с экспоненциальным backoff — при одновременном старте нескольких sandbox-сессий ордера
    упираются в лимит API. HTTP 401/400 и прочие НЕ ретраим (постоянные ошибки).
    """
    url = f"{_HOST}/{service}/{method}"
    data = json.dumps(body).encode("utf-8")
    last_err: Exception | None = None
    for attempt in range(_retries):
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=60, context=_ssl_ctx()) as r:  # noqa: S310
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 429 (rate limit) — транзиентная: ждём и ретраим (backoff растёт). Прочие HTTP — сразу.
            if e.code == 429 and attempt < _retries - 1:
                last_err = e
                time.sleep(2.0 * (attempt + 1))
                continue
            detail = e.read().decode("utf-8", "replace")[:400]
            raise TBankError(f"{method} → HTTP {e.code}: {detail}") from None
        except Exception as e:  # noqa: BLE001  IncompleteRead/URLError/timeout — ретраим
            last_err = e
            if attempt < _retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise TBankError(f"{method} → запрос не прошёл после {_retries} попыток: {last_err}")


def _q(units, nano=0) -> dict:
    """Quotation {units, nano}. nano — 1e-9 доля."""
    return {"units": str(int(units)), "nano": int(nano)}


def _q_to_float(q: dict | None) -> float:
    if not q:
        return 0.0
    return int(q.get("units", 0)) + int(q.get("nano", 0)) / 1e9


# ---------------------------------------------------------------- инструменты

def find_future(ticker: str) -> dict:
    """Найти фьючерс по тикеру через InstrumentsService.Futures + фильтр.

    Возвращает запись с figi, uid, lot, флагами торгуемости, min_price_increment.
    """
    resp = _call(_INSTRUMENTS, "Futures", {"instrumentStatus": "INSTRUMENT_STATUS_ALL"})
    for it in resp.get("instruments", []):
        if it.get("ticker") == ticker:
            return it
    raise TBankError(f"фьючерс {ticker} не найден в справочнике")


def find_share(ticker: str) -> dict:
    """Найти АКЦИЮ по тикеру через InstrumentsService.Shares + фильтр (для st6).

    Возвращает запись того же формата, что find_future: figi, uid, lot (РАЗНЫЙ у акций —
    NLMK=10, CHMF=1), min_price_increment, флаги торгуемости (apiTradeAvailableFlag и пр.).
    instrumentStatus=INSTRUMENT_STATUS_BASE — только базовый (торгуемый) список бумаг.
    """
    resp = _call(_INSTRUMENTS, "Shares", {"instrumentStatus": "INSTRUMENT_STATUS_BASE"})
    for it in resp.get("instruments", []):
        if it.get("ticker") == ticker:
            return it
    raise TBankError(f"акция {ticker} не найдена в справочнике")


# код интервала ТЗ (минуты) → enum CandleInterval T-Bank
_CANDLE_INTERVAL = {1: "CANDLE_INTERVAL_1_MIN", 5: "CANDLE_INTERVAL_5_MIN",
                    10: "CANDLE_INTERVAL_10_MIN", 60: "CANDLE_INTERVAL_HOUR"}


def get_candles(instrument_id: str, interval_min: int, frm_iso: str, to_iso: str,
                only_closed: bool = True) -> list[tuple[int, float]]:
    """REAL-TIME свечи T-Bank: список (ts_ms_utc, close) по ЗАКРЫТЫМ барам.

    interval_min — 1/5/10/60. frm_iso/to_iso — UTC ISO8601 ('...Z'). only_closed=True
    отбрасывает формирующийся бар (isComplete=False) — индикаторы по закрытым (no repaint).
    В отличие от MOEX ISS, данные T-Bank без задержки (последний закрытый бар — текущий).
    """
    from datetime import datetime
    iv = _CANDLE_INTERVAL.get(interval_min, "CANDLE_INTERVAL_10_MIN")
    resp = _call(_MARKETDATA, "GetCandles",
                 {"instrumentId": instrument_id, "from": frm_iso, "to": to_iso, "interval": iv})
    out: list[tuple[int, float]] = []
    for c in resp.get("candles", []):
        if only_closed and not c.get("isComplete", False):
            continue
        # time — UTC ISO8601 (напр. '2026-06-10T11:50:00Z')
        t = c["time"].replace("Z", "+00:00")
        ts = int(datetime.fromisoformat(t).timestamp() * 1000)
        out.append((ts, _q_to_float(c.get("close"))))
    return out


def describe_instrument(it: dict) -> str:
    api_ok = it.get("apiTradeAvailableFlag")
    buy_sell = it.get("buyAvailableFlag"), it.get("sellAvailableFlag")
    return (f"{it['ticker']:6} {it.get('name','')[:32]:32} "
            f"figi={it.get('figi')} lot={it.get('lot')} "
            f"шаг={_q_to_float(it.get('minPriceIncrement'))} "
            f"API-торговля={'ДА' if api_ok else 'НЕТ'} buy/sell={buy_sell} "
            f"эксп={it.get('expirationDate','')[:10]}")


def last_price(figi: str) -> float:
    resp = _call(_MARKETDATA, "GetLastPrices", {"instrumentId": [figi]})
    lp = resp.get("lastPrices", [])
    return _q_to_float(lp[0]["price"]) if lp else 0.0


def order_book(instrument_id: str, depth: int = 10) -> dict:
    """Биржевой стакан (DOM): уровни bid/ask с объёмами. instrument_id — uid/figi серии.
    Возвращает {bids:[{price,qty}], asks:[{price,qty}], last}. bids — по убыванию цены,
    asks — по возрастанию (как отдаёт T-Bank)."""
    resp = _call(_MARKETDATA, "GetOrderBook", {"instrumentId": instrument_id, "depth": depth})
    def _lvls(key):
        return [{"price": _q_to_float(r.get("price")), "qty": int(r.get("quantity", 0))}
                for r in resp.get(key, [])]
    return {"bids": _lvls("bids"), "asks": _lvls("asks"),
            "last": _q_to_float(resp.get("lastPrice"))}


def is_tradable(instrument_id: str) -> bool:
    """Доступен ли инструмент для торгов СЕЙЧАС (tradingStatus + флаги ордеров).
    Чтобы не слать ордер в неторговое время (клиринг/ночь/выходные) → HTTP400 30079."""
    try:
        resp = _call(_MARKETDATA, "GetTradingStatus", {"instrumentId": instrument_id})
        st = resp.get("tradingStatus", "")
        # допускаем нормальную и dealer-сессии; ордера должны быть разрешены
        ok_status = st in ("SECURITY_TRADING_STATUS_NORMAL_TRADING",
                           "SECURITY_TRADING_STATUS_DEALER_NORMAL_TRADING")
        return bool(ok_status and resp.get("marketOrderAvailableFlag", True)
                    and resp.get("apiTradeAvailableFlag", True))
    except Exception:  # noqa: BLE001  не смогли узнать статус — не блокируем (как было)
        return True


# ---------------------------------------------------------------- sandbox

def open_account(name: str = "st4-spread-sandbox") -> str:
    return _call(_SANDBOX, "OpenSandboxAccount", {"name": name})["accountId"]


def list_accounts() -> list[dict]:
    return _call(_SANDBOX, "GetSandboxAccounts", {}).get("accounts", [])


def pay_in(account_id: str, rub: int) -> float:
    resp = _call(_SANDBOX, "SandboxPayIn", {
        "accountId": account_id,
        "amount": {"currency": "rub", "units": str(int(rub)), "nano": 0},
    })
    return _q_to_float(resp.get("balance"))


def post_order(account_id: str, instrument_uid: str, lots: int, direction: str,
               order_id: str, order_type: str = "ORDER_TYPE_MARKET",
               price: dict | None = None) -> dict:
    """Поставить ордер в ПЕСОЧНИЦЕ. direction: ORDER_DIRECTION_BUY|SELL."""
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
    return _call(_SANDBOX, "PostSandboxOrder", body)


def portfolio(account_id: str) -> dict:
    return _call(_SANDBOX, "GetSandboxPortfolio", {"accountId": account_id})


def positions(account_id: str) -> dict:
    return _call(_SANDBOX, "GetSandboxPositions", {"accountId": account_id})


def blocked_margin(account_id: str) -> float:
    """РЕАЛЬНО заблокированное ГО под открытые фьючерсные позиции (₽, с хедж-скидкой биржи).

    T-Bank не отдаёт ГО отдельным полем. На FORTS ГО списывается с денежной позиции при
    открытии фьючерса: заблокировано = (сумма денег без позиций) − (свободные деньги сейчас).
    Считаем как: totalAmountPortfolio − свободный рублёвый баланс (money-позиция).
    Возвращает 0, если фьючерсных позиций нет. Точно только при ОТКРЫТЫХ позициях.
    """
    try:
        pf = _call(_SANDBOX, "GetSandboxPortfolio", {"accountId": account_id})
        pos = _call(_SANDBOX, "GetSandboxPositions", {"accountId": account_id})
    except Exception:  # noqa: BLE001
        return 0.0
    has_fut = any(int(float(f.get("balance", 0))) != 0 for f in pos.get("futures", []))
    if not has_fut:
        return 0.0
    total = _q_to_float(pf.get("totalAmountPortfolio"))
    # свободные деньги = рублёвая money-позиция (не заблокированная под ГО)
    free = 0.0
    for m in pos.get("money", []):
        if m.get("currency") == "rub":
            free = _q_to_float(m)
    blocked = total - free
    return max(0.0, blocked)


def close_account(account_id: str) -> None:
    _call(_SANDBOX, "CloseSandboxAccount", {"accountId": account_id})


def last_entry_ts_for(account_id: str, instrument_uid: str, days: int = 7) -> int | None:
    """История операций sandbox через REST недоступна (GetOperations→401, GetSandboxOperations
    →404). Возвращаем None → caller делает fallback на last_live_ts (время последнего бара)."""
    return None


# ---------------------------------------------------------------- сценарий

def _uid(it: dict) -> str:
    """Идентификатор инструмента для ордера: uid предпочтительнее figi."""
    return it.get("uid") or it.get("figi")


def check_instruments() -> tuple[dict, dict]:
    """Только проверка: найти SRM6/SPM6, показать флаги и стакан. Без ордеров."""
    print("=== Справочник инструментов T-Bank ===")
    ord_i = find_future(TICKER_ORD)
    pref_i = find_future(TICKER_PREF)
    print(describe_instrument(ord_i))
    print(describe_instrument(pref_i))
    print(f"\nпоследняя цена {TICKER_ORD}: {last_price(ord_i['figi']):.0f}")
    print(f"последняя цена {TICKER_PREF}: {last_price(pref_i['figi']):.0f}")
    spread = last_price(pref_i["figi"]) - last_price(ord_i["figi"])
    print(f"спред SBPR−SBRF: {spread:+.0f}")
    if not (ord_i.get("apiTradeAvailableFlag") and pref_i.get("apiTradeAvailableFlag")):
        print("\n⚠ ВНИМАНИЕ: торговля через API недоступна по одной из ног — sandbox-прогон может не пройти")
    return ord_i, pref_i


def run_pair_roundtrip(lots: int = 1, payin_rub: int = 200_000) -> None:
    """Полный sandbox-прогон: счёт → пополнение → шорт спреда → позиция → закрытие → итог."""
    ord_i, pref_i = check_instruments()

    # переиспользуем существующий sandbox-счёт st4, иначе открываем новый
    accs = list_accounts()
    acc = next((a for a in accs if a.get("name") == "st4-spread-sandbox"
                and a.get("status") == "ACCOUNT_STATUS_OPEN"), None)
    account_id = acc["id"] if acc else open_account()
    print(f"\n=== Sandbox-счёт {account_id} ===")
    bal = pay_in(account_id, payin_rub)
    print(f"пополнено, баланс ₽: {bal:.0f}")

    # ШОРТ СПРЕДА (как в st4/engine): SELL-сигнал → buy SBRF + sell SBPR
    # orderId должен быть валидным UUID (требование T-Bank API)
    print(f"\n=== Вход: ШОРТ спреда ({lots} лот) — BUY {TICKER_ORD} + SELL {TICKER_PREF} ===")
    r1 = post_order(account_id, _uid(ord_i), lots, "ORDER_DIRECTION_BUY", str(uuid.uuid4()))
    r2 = post_order(account_id, _uid(pref_i), lots, "ORDER_DIRECTION_SELL", str(uuid.uuid4()))
    print(f"  {TICKER_ORD}  BUY : status={r1.get('executionReportStatus')} "
          f"avg={_q_to_float(r1.get('executedOrderPrice')):.0f}")
    print(f"  {TICKER_PREF} SELL: status={r2.get('executionReportStatus')} "
          f"avg={_q_to_float(r2.get('executedOrderPrice')):.0f}")

    _print_positions(account_id)

    # ВЫХОД: обратные ордера
    print("\n=== Выход: закрытие обеих ног ===")
    r3 = post_order(account_id, _uid(ord_i), lots, "ORDER_DIRECTION_SELL", str(uuid.uuid4()))
    r4 = post_order(account_id, _uid(pref_i), lots, "ORDER_DIRECTION_BUY", str(uuid.uuid4()))
    print(f"  {TICKER_ORD}  SELL: status={r3.get('executionReportStatus')} "
          f"avg={_q_to_float(r3.get('executedOrderPrice')):.0f}")
    print(f"  {TICKER_PREF} BUY : status={r4.get('executionReportStatus')} "
          f"avg={_q_to_float(r4.get('executedOrderPrice')):.0f}")

    pf = portfolio(account_id)
    total = _q_to_float(pf.get("totalAmountPortfolio"))
    expected = _q_to_float(pf.get("expectedYield"))
    print("\n=== Итог ===")
    print(f"стоимость портфеля ₽: {total:.0f}")
    print(f"ожидаемая доходность %: {expected:.2f}")
    print("(виртуальные деньги, реальные ордера в песочнице — реальная торговля НЕ затронута)")


def _print_positions(account_id: str) -> None:
    pos = positions(account_id)
    futs = pos.get("futures", [])
    print("позиции (фьючерсы):")
    if not futs:
        print("  — (ещё не отразились, повтор через 1с)")
        time.sleep(1)
        futs = positions(account_id).get("futures", [])
    for f in futs:
        print(f"  {f.get('instrumentUid','')[:8]}… баланс={f.get('balance')} "
              f"заблок={f.get('blocked')}")


def main(argv: list[str]) -> int:
    try:
        if "--check" in argv:
            check_instruments()
        else:
            run_pair_roundtrip()
        return 0
    except TBankError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
