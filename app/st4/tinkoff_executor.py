"""Реальный (sandbox) исполнитель пары через T-Bank Invest API.

TinkoffSandboxExecutor реализует тот же интерфейс, что и paper-OrderExecutor
(execute_pair + close_pair), но ставит ордера в ПЕСОЧНИЦЕ T-Bank через tbank_sandbox.py.
Engine не знает про figi/uid/счёт — вся реальная идентификация инкапсулирована здесь.

БЕЗОПАСНОСТЬ: ходит ИСКЛЮЧИТЕЛЬНО через функции tbank_sandbox (только SandboxService.*).
Боевой OrdersService не импортируется и не существует в клиенте — реальный ордер
отправить нельзя. Токен — из окружения процесса (TBANK_TOKEN), не на диске.
"""
from __future__ import annotations

import time
import uuid

from . import tbank_live, tbank_sandbox
from .config import ConnectorConfig, ExecutionConfig
from .execution import Fill, PairCloseResult, PairFillResult, UnwindError
from .models import InstrumentSpec, Position, Role
from .tbank_sandbox import TBankError

_FILL_OK = "EXECUTION_REPORT_STATUS_FILL"


class TinkoffSandboxExecutor:
    """Исполнитель пары SBRF/SBPR в песочнице T-Bank (market-ордера)."""

    def __init__(self, exec_cfg: ExecutionConfig, conn_cfg: ConnectorConfig,
                 spec_ord: InstrumentSpec, spec_pref: InstrumentSpec, sb=None) -> None:
        self.cfg = exec_cfg
        self.conn = conn_cfg
        self.spec = {Role.ORDINARY: spec_ord, Role.PREFERRED: spec_pref}
        # sb — модуль-зависимость (инъекция для моков). По умолчанию — реальный tbank_sandbox;
        # читаем из глобали в теле (а не дефолтом аргумента), чтобы monkeypatch работал.
        self.sb = sb if sb is not None else tbank_sandbox
        self._account_id: str = ""
        self._inst: dict[Role, dict] = {}     # кэш справочника инструментов по роли
        self._ensure_started()

    # ---------- инициализация ----------
    def _resolve_instruments(self) -> None:
        """Найти фьючерсы в справочнике T-Bank по тикеру (= SECID FORTS = spec.code), кэшировать."""
        if self._inst:
            return
        for role in (Role.ORDINARY, Role.PREFERRED):
            it = self.sb.find_future(self.spec[role].code)
            self._inst[role] = it

    def leg_uids(self) -> tuple[str, str]:
        """uid обеих ног (SBRF, SBPR) для запроса real-time свечей T-Bank."""
        return self.sb._uid(self._inst[Role.ORDINARY]), self.sb._uid(self._inst[Role.PREFERRED])

    def is_tradable(self) -> bool:
        """Торгуются ли ОБЕ ноги сейчас (для гейта неторгового времени в движке).
        Обе должны быть доступны — парный ордер атомарен."""
        try:
            self._resolve_instruments()
            uid_o = self.sb._uid(self._inst[Role.ORDINARY])
            uid_p = self.sb._uid(self._inst[Role.PREFERRED])
            return self.sb.is_tradable(uid_o) and self.sb.is_tradable(uid_p)
        except Exception:  # noqa: BLE001  не смогли проверить — не блокируем
            return True

    def _account(self) -> str:
        """Переиспользовать sandbox-счёт (conn.account_id / по имени) или открыть новый."""
        accs = self.sb.list_accounts()
        # 1) по сохранённому id
        if self.conn.account_id:
            for a in accs:
                if a.get("id") == self.conn.account_id and a.get("status") == "ACCOUNT_STATUS_OPEN":
                    return self.conn.account_id
        # 2) по имени счёта
        for a in accs:
            if a.get("name") == self.conn.account_name and a.get("status") == "ACCOUNT_STATUS_OPEN":
                return a["id"]
        # 3) открыть новый
        return self.sb.open_account(self.conn.account_name)

    def _ensure_started(self) -> None:
        """Резолв инструментов + счёт + пополнение под ГО ТОЛЬКО при нехватке средств.

        Раньше pay_in вызывался при КАЖДОМ старте live → sandbox-счёт раздувался (десятки млн),
        и частые SandboxPayIn ловили периодический HTTP 500 (T-Bank internal 70001) → откат в
        paper. Пополняем, только если денег на счёте меньше payin_rub."""
        self._resolve_instruments()
        self._account_id = self._account()
        self.conn.account_id = self._account_id    # запомнить для сериализации/переиспользования
        need = self.conn.payin_rub
        try:
            pf = self.sb.portfolio(self._account_id)
            cash = self.sb._q_to_float(pf.get("totalAmountCurrencies"))
        except Exception:  # noqa: BLE001  не смогли прочитать баланс — пополним как раньше
            cash = 0.0
        if cash < need:
            self.sb.pay_in(self._account_id, need)

    # ---------- одна нога ----------
    def _post_leg(self, role: Role, side: str, lots: int, ref: float) -> Fill | None:
        """Поставить ОДНУ market-ногу в песочнице. None — не исполнилась (реджект/частичный)."""
        it = self._inst[role]
        spec = self.spec[role]
        direction = "ORDER_DIRECTION_BUY" if side == "buy" else "ORDER_DIRECTION_SELL"
        resp = self.sb.post_order(self._account_id, self.sb._uid(it), lots, direction,
                                  str(uuid.uuid4()), order_type="ORDER_TYPE_MARKET")
        status = resp.get("executionReportStatus")
        # executedOrderPrice — СУММА за все лоты (не цена за контракт!). Делим на исполненные
        # лоты, иначе при lots>1 цена входа завышается в N раз → искажённый P&L и стопы.
        executed = int(resp.get("lotsExecuted") or lots) or lots
        avg = self.sb._q_to_float(resp.get("executedOrderPrice")) / executed
        if status != _FILL_OK or avg <= 0:
            return None
        slip = (avg - ref) / spec.tick_size * (1 if side == "buy" else -1)
        # lots в Fill — ФАКТИЧЕСКИ исполненные: при частичном филле размер позиции
        # должен совпадать с брокером (рассинхрон ног ловит execute_pair)
        return Fill(code=spec.code, role=spec.role, side=side, lots=executed,
                    avg_price=avg, reference_price=ref, slippage_ticks=slip)

    def _retry_leg(self, role: Role, side: str, lots: int, ref: float) -> Fill | None:
        for _ in range(self.cfg.max_retries):
            f = self._post_leg(role, side, lots, ref)
            if f is not None:
                return f
        return None

    # ---------- вход (атомарность/unwind) ----------
    def execute_pair(self, buy_ord: bool, buy_pref: bool, lots: int,
                     book_ord: tuple[float, float], book_pref: tuple[float, float],
                     ref_ord: float, ref_pref: float) -> PairFillResult:
        """Атомарный парный вход в песочнице (структура как paper OrderExecutor §10.3)."""
        side_ord = "buy" if buy_ord else "sell"
        side_pref = "buy" if buy_pref else "sell"
        first_pref = self.cfg.first_leg_to_fill == "Preferred"

        # порядок: менее ликвидную (Preferred) первой
        if first_pref:
            f1 = self._retry_leg(Role.PREFERRED, side_pref, lots, ref_pref)
        else:
            f1 = self._retry_leg(Role.ORDINARY, side_ord, lots, ref_ord)
        if f1 is None:
            return PairFillResult(ok=False, aborted=True,
                                  reason="первая нога (sandbox) не исполнилась — вход отменён")

        if first_pref:
            f2 = self._retry_leg(Role.ORDINARY, side_ord, lots, ref_ord)
        else:
            f2 = self._retry_leg(Role.PREFERRED, side_pref, lots, ref_pref)
        if f2 is None:
            # аварийный unwind первой ноги обратным market-ордером
            if not self._unwind(f1):
                raise UnwindError("вторая нога sandbox не залилась, unwind первой не удался")
            return PairFillResult(ok=False, unwound=True,
                                  reason="вторая нога (sandbox) не исполнилась — первая закрыта (unwind)")

        if f1.lots != f2.lots:
            # частичный филл одной из ног — рассинхрон размеров недопустим: закрываем обе
            ok1 = self._unwind(f1)
            ok2 = self._unwind(f2)
            if not (ok1 and ok2):
                raise UnwindError("частичный филл ноги, аварийное закрытие не удалось")
            return PairFillResult(ok=False, unwound=True,
                                  reason=f"частичный филл ({f1.lots}≠{f2.lots} лотов) — обе ноги закрыты")

        if first_pref:
            return PairFillResult(ok=True, fill_pref=f1, fill_ord=f2)
        return PairFillResult(ok=True, fill_ord=f1, fill_pref=f2)

    def _unwind(self, leg: Fill) -> bool:
        """Закрыть уже открытую первую ногу обратным market-ордером."""
        close_side = "sell" if leg.side == "buy" else "buy"
        f = self._post_leg(leg.role, close_side, leg.lots, leg.avg_price)
        return f is not None

    # ---------- выход (реальный обратный ордер) ----------
    def close_pair(self, pos: Position, ref_ord: float, ref_pref: float) -> PairCloseResult:
        """Реальный выход: обратные market-ордера по обеим ногам. UnwindError если не закрылись."""
        # закрытие = противоположная сторона входа каждой ноги
        ord_close = "sell" if pos.leg_ord.side == "buy" else "buy"
        pref_close = "sell" if pos.leg_pref.side == "buy" else "buy"
        lots = pos.leg_ord.lots
        f_ord = self._retry_leg(Role.ORDINARY, ord_close, lots, ref_ord)
        f_pref = self._retry_leg(Role.PREFERRED, pref_close, lots, ref_pref)
        if f_ord is None or f_pref is None:
            # одна нога не закрылась — голая позиция недопустима
            raise UnwindError("не удалось закрыть ногу в sandbox при выходе (голая позиция)")
        slip = abs(f_ord.slippage_ticks) + abs(f_pref.slippage_ticks)
        return PairCloseResult(exit_ord=f_ord.avg_price, exit_pref=f_pref.avg_price,
                               slippage_ticks=slip)

    # ---------- reconciliation (§11) ----------
    def broker_lots(self) -> dict[Role, int]:
        """Фактические лоты ног на sandbox-счёте T-Bank (balance по uid). Для сверки на старте."""
        uid_o = self.sb._uid(self._inst[Role.ORDINARY])
        uid_p = self.sb._uid(self._inst[Role.PREFERRED])
        out = {Role.ORDINARY: 0, Role.PREFERRED: 0}
        for f in self.sb.positions(self._account_id).get("futures", []):
            uid = f.get("instrumentUid", "")
            bal = int(f.get("balance", 0))
            if uid == uid_o:
                out[Role.ORDINARY] = bal
            elif uid == uid_p:
                out[Role.PREFERRED] = bal
        return out

    def entry_prices(self) -> dict[Role, float]:
        """Средние цены входа ног на sandbox-счёте (averagePositionPrice из портфеля).

        Для восстановления позиции движка из счёта при рестарте: лоты даёт broker_lots,
        цены входа — отсюда. 0.0 если ноги нет в портфеле."""
        uid_o = self.sb._uid(self._inst[Role.ORDINARY])
        uid_p = self.sb._uid(self._inst[Role.PREFERRED])
        out = {Role.ORDINARY: 0.0, Role.PREFERRED: 0.0}
        for f in self.sb.portfolio(self._account_id).get("positions", []):
            uid = f.get("instrumentUid", "")
            avg = self.sb._q_to_float(f.get("averagePositionPrice"))
            if uid == uid_o:
                out[Role.ORDINARY] = avg
            elif uid == uid_p:
                out[Role.PREFERRED] = avg
        return out

    def broker_entry_ts(self) -> int | None:
        """Реальное время (unix ms) последней сделки по любой ноге — для усыновления позиции
        со счёта с КОРРЕКТНЫМ entry_ts (а не моментом рестарта). None, если недоступно
        (sandbox: история операций недоступна → caller сделает fallback на last_live_ts)."""
        if not hasattr(self.sb, "last_entry_ts_for"):
            return None
        best = None
        for role in (Role.ORDINARY, Role.PREFERRED):
            uid = self.sb._uid(self._inst[role])
            try:
                ts = self.sb.last_entry_ts_for(self._account_id, uid)
            except Exception:  # noqa: BLE001
                ts = None
            if ts and (best is None or ts > best):
                best = ts
        return best

    def flat_broker(self) -> bool:
        """Закрыть ВСЕ реальные позиции на sandbox-счёте по рынку (привести к FLAT).

        Для устранения рассинхрона на старте: если на счёте висят ноги, а движок их не знает.
        """
        lots = self.broker_lots()
        ok = True
        for role, bal in lots.items():
            if bal == 0:
                continue
            side = "sell" if bal > 0 else "buy"   # закрыть в противоположную сторону
            f = self._retry_leg(role, side, abs(bal), 0.0)
            ok = ok and (f is not None)
        return ok


class TinkoffLiveExecutor(TinkoffSandboxExecutor):
    """⚠️ БОЕВОЙ исполнитель пары — реальные деньги через OrdersService.

    Зеркало sandbox-исполнителя (вся логика атомарности/unwind/reconciliation наследуется),
    но: (1) ходит через tbank_live (боевые сервисы), (2) НЕ открывает и НЕ пополняет счёт —
    использует существующий реальный account_id, проверяя что он открыт, (3) идемпотентный
    orderId (повтор при сетевом обрыве не задвоит ордер), (4) ВХОДЫ (execute_pair) отправляются
    только если взведён флаг реальной торговли (armed_cb) — двойной включатель сверх движка.
    Закрытие (close_pair/unwind/flat_broker) гейтом НЕ ограничено — снижение риска разрешено всегда.
    """

    def __init__(self, exec_cfg: ExecutionConfig, conn_cfg: ConnectorConfig,
                 spec_ord: InstrumentSpec, spec_pref: InstrumentSpec, sb=None,
                 armed_cb=None) -> None:
        # armed_cb() -> bool — взведена ли реальная торговля (state['real_trading_armed']).
        # Если None — считаем НЕ взведённым (safe-by-default): входы не пойдут.
        self._armed_cb = armed_cb or (lambda: False)
        # sb по умолчанию — боевой модуль tbank_live (а не sandbox).
        super().__init__(exec_cfg, conn_cfg, spec_ord, spec_pref,
                         sb=sb if sb is not None else tbank_live)

    def _account(self) -> str:
        """Реальный счёт: ТОЛЬКО проверка существующего account_id (НЕ открываем новый)."""
        if not self.conn.account_id:
            raise TBankError("боевой режим: account_id не задан (нужен явный реальный счёт)")
        if not self.sb.account_is_open(self.conn.account_id):
            raise TBankError(f"боевой режим: счёт {self.conn.account_id} не найден или закрыт")
        return self.conn.account_id

    def _ensure_started(self) -> None:
        """Резолв инструментов + проверка реального счёта. БЕЗ pay_in (реальный счёт не пополняем)."""
        self._resolve_instruments()
        self._account_id = self._account()
        self.conn.account_id = self._account_id

    def _post_leg(self, role: Role, side: str, lots: int, ref: float) -> Fill | None:
        """Одна БОЕВАЯ нога с идемпотентным orderId (повтор не задвоит ордер)."""
        it = self._inst[role]
        spec = self.spec[role]
        direction = "ORDER_DIRECTION_BUY" if side == "buy" else "ORDER_DIRECTION_SELL"
        uid = self.sb._uid(it)
        order_id = self.sb.make_order_id(self._account_id, uid, lots, direction, time.time())
        resp = self.sb.post_order(self._account_id, uid, lots, direction,
                                  order_id, order_type="ORDER_TYPE_MARKET")
        status = resp.get("executionReportStatus")
        executed = int(resp.get("lotsExecuted") or lots) or lots
        avg = self.sb._q_to_float(resp.get("executedOrderPrice")) / executed
        if status != _FILL_OK or avg <= 0:
            return None
        slip = (avg - ref) / spec.tick_size * (1 if side == "buy" else -1)
        return Fill(code=spec.code, role=spec.role, side=side, lots=executed,
                    avg_price=avg, reference_price=ref, slippage_ticks=slip)

    def execute_pair(self, *args, **kwargs) -> PairFillResult:
        """Боевой ВХОД — только при взведённом флаге реальной торговли (двойной включатель)."""
        if not self._armed_cb():
            return PairFillResult(ok=False, aborted=True,
                                  reason="реальная торговля не взведена — боевой вход заблокирован")
        return super().execute_pair(*args, **kwargs)
