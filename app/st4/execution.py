"""Исполнение парного ордера (paper-модель §10) + расчёт P&L по тикам.

OrderExecutor имитирует FORTS, где нет нативного атомарного парного ордера: ноги
исполняются последовательно (сначала менее ликвидная — Preferred), со сверкой. При
неисполнении второй ноги — аварийный unwind уже открытой первой; если и он не удался —
сигнал HALTED наверх. Частичных филлов в paper нет (лимитный по marketable-цене берём
целиком), но интерфейс это допускает.

P&L ноги = (exit − entry)·направление·lots·(TickValue/TickSize). Стоимость шага у
SBRF/SBPR берётся из InstrumentSpec, не хардкодится (§9.5). На размер лота НЕ умножаем —
TickValue (STEPPRICE) уже на целый контракт.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .config import ExecutionConfig, Paper
from .models import Fill, InstrumentSpec, LegPosition, Position, Role


class UnwindError(RuntimeError):
    """Вторую ногу не залили и аварийный unwind первой тоже не удался → HALTED."""


@dataclass
class PairFillResult:
    """Результат исполнения пары: обе ноги или аварийный исход."""
    ok: bool
    fill_ord: Fill | None = None
    fill_pref: Fill | None = None
    aborted: bool = False        # первая нога не залилась — позиции нет, чисто
    unwound: bool = False        # вторая не залилась, первую закрыли — чисто
    reason: str = ""


@dataclass
class PairCloseResult:
    """Фактические цены выхода обеих ног (для P&L закрытия)."""
    exit_ord: float              # цена выхода ноги SBRF (пункты)
    exit_pref: float             # цена выхода ноги SBPR
    slippage_ticks: float = 0.0


class PairExecutor(Protocol):
    """Интерфейс исполнителя пары: paper (OrderExecutor) и sandbox (TinkoffSandboxExecutor).

    Engine зависит только от этого контракта — выбор реализации делается по
    cfg.connector.mode в TradingEngine.__init__.
    """

    def execute_pair(self, buy_ord: bool, buy_pref: bool, lots: int,
                     book_ord: tuple[float, float], book_pref: tuple[float, float],
                     ref_ord: float, ref_pref: float) -> PairFillResult: ...

    def close_pair(self, pos: Position, ref_ord: float,
                   ref_pref: float) -> PairCloseResult: ...


class OrderExecutor:
    """Paper-исполнитель парного ордера с атомарностью и unwind (§10)."""

    def __init__(self, exec_cfg: ExecutionConfig, paper: Paper,
                 spec_ord: InstrumentSpec, spec_pref: InstrumentSpec) -> None:
        self.cfg = exec_cfg
        self.paper = paper
        self.spec = {Role.ORDINARY: spec_ord, Role.PREFERRED: spec_pref}
        self._fail_seq = 0       # счётчик для детерминированной модели неисполнения в тестах

    def _limit_price(self, spec: InstrumentSpec, side: str, bid: float, ask: float) -> float:
        """Цена лимитного ордера (§10.1). MarketableLimit заходит за спред на tick_offset."""
        off = self.cfg.tick_offset * spec.tick_size
        if self.cfg.entry_style == "MarketableLimit":
            return ask + off if side == "buy" else bid - off
        return bid if side == "buy" else ask   # Passive

    def _try_fill_leg(self, spec: InstrumentSpec, side: str, lots: int,
                      bid: float, ask: float, reference: float) -> Fill | None:
        """Имитация исполнения ноги. None — нога не залилась за max_retries.

        Защита от ухода цены (§10.1): если расчётная цена ушла от reference дальше
        deviation_protection_ticks — не входим (None). Модель неисполнения управляется
        paper_fill_fail_prob (детерминированно по счётчику — для тестов unwind).
        """
        limit = self._limit_price(spec, side, bid, ask)
        # защита от ухода: отклонение лимитной цены от ожидаемой
        if abs(limit - reference) > self.cfg.deviation_protection_ticks * spec.tick_size:
            return None
        # детерминированная модель неисполнения: каждый k-й вызов «не зальётся»
        if self.cfg.paper_fill_fail_prob > 0:
            self._fail_seq += 1
            period = max(1, round(1.0 / self.cfg.paper_fill_fail_prob))
            if self._fail_seq % period == 0:
                return None
        # marketable-limit заливается целиком по лимитной цене; проскальзывание = limit−reference
        slip = (limit - reference) / spec.tick_size * (1 if side == "buy" else -1)
        return Fill(code=spec.code, role=spec.role, side=side, lots=lots,
                    avg_price=limit, reference_price=reference, slippage_ticks=slip)

    def execute_pair(self, buy_ord: bool, buy_pref: bool, lots: int,
                     book_ord: tuple[float, float], book_pref: tuple[float, float],
                     ref_ord: float, ref_pref: float) -> PairFillResult:
        """Исполнить пару ног последовательно с атомарностью (§10.3).

        book_* = (bid, ask). ref_* — расчётная цена-ориентир (close бара) для проскальзывания.
        Порядок: сначала менее ликвидная (first_leg_to_fill, по умолчанию Preferred).
        """
        side_ord = "buy" if buy_ord else "sell"
        side_pref = "buy" if buy_pref else "sell"
        first_pref = self.cfg.first_leg_to_fill == "Preferred"

        def fill_first():
            if first_pref:
                return self._try_fill_leg(self.spec[Role.PREFERRED], side_pref, lots,
                                          book_pref[0], book_pref[1], ref_pref)
            return self._try_fill_leg(self.spec[Role.ORDINARY], side_ord, lots,
                                      book_ord[0], book_ord[1], ref_ord)

        def fill_second():
            if first_pref:
                return self._try_fill_leg(self.spec[Role.ORDINARY], side_ord, lots,
                                          book_ord[0], book_ord[1], ref_ord)
            return self._try_fill_leg(self.spec[Role.PREFERRED], side_pref, lots,
                                      book_pref[0], book_pref[1], ref_pref)

        # ретраи первой ноги
        f1 = None
        for _ in range(self.cfg.max_retries):
            f1 = fill_first()
            if f1 is not None:
                break
        if f1 is None:
            return PairFillResult(ok=False, aborted=True,
                                  reason="первая нога не исполнилась — вход отменён")

        # ретраи второй ноги
        f2 = None
        for _ in range(self.cfg.max_retries):
            f2 = fill_second()
            if f2 is not None:
                break
        if f2 is None:
            # аварийный unwind первой ноги (закрыть в противоположную сторону)
            unwound = self._unwind(f1, book_ord, book_pref)
            if not unwound:
                raise UnwindError("вторая нога не залилась, unwind первой не удался")
            return PairFillResult(ok=False, unwound=True,
                                  reason="вторая нога не исполнилась — первая нога закрыта (unwind)")

        if first_pref:
            return PairFillResult(ok=True, fill_pref=f1, fill_ord=f2)
        return PairFillResult(ok=True, fill_ord=f1, fill_pref=f2)

    def _unwind(self, leg: Fill, book_ord: tuple[float, float],
                book_pref: tuple[float, float]) -> bool:
        """Закрыть уже открытую первую ногу marketable-limit'ом (§10.3.2)."""
        spec = self.spec[leg.role]
        close_side = "sell" if leg.side == "buy" else "buy"
        book = book_pref if leg.role == Role.PREFERRED else book_ord
        # unwind без модели неисполнения — приоритет «не остаться с голой ногой»
        off = self.cfg.tick_offset * spec.tick_size
        limit = book[1] + off if close_side == "buy" else book[0] - off
        return limit > 0

    def close_pair(self, pos: Position, ref_ord: float, ref_pref: float) -> PairCloseResult:
        """Paper-выход: платим полспреда книги + tick_offset, СИММЕТРИЧНО входу.

        Раньше выход исполнялся ровно по close (бесплатно) — round-trip занижался на
        ~(halfspread+offset)·2 тика. Sandbox переопределяет реальным обратным ордером.
        """
        def px(spec: InstrumentSpec, entry_side: str, ref: float) -> float:
            cost = (self.cfg.paper_book_halfspread_ticks + self.cfg.tick_offset) * spec.tick_size
            close_side = "sell" if entry_side == "buy" else "buy"
            return ref + cost if close_side == "buy" else ref - cost

        return PairCloseResult(
            exit_ord=px(self.spec[Role.ORDINARY], pos.leg_ord.side, ref_ord),
            exit_pref=px(self.spec[Role.PREFERRED], pos.leg_pref.side, ref_pref))


def leg_pnl_rub(leg: LegPosition, exit_price: float, spec: InstrumentSpec) -> float:
    """P&L ноги в рублях (§9.5): (exit − entry)·dir·lots·(TickValue/TickSize).

    TickValue (STEPPRICE) на FORTS — рублёвая стоимость шага ЦЕЛОГО контракта (размер лота
    LOTVOLUME уже зашит биржей), поэтому на spec.lot домножать нельзя — это двойной учёт.
    Сверено с varMargin sandbox T-Bank: пункты·lots·(STEPPRICE/MINSTEP) совпадает с биржей.
    """
    direction = 1.0 if leg.side == "buy" else -1.0
    points = (exit_price - leg.entry_price) * direction
    return points * leg.lots * (spec.tick_value_rub / spec.tick_size)


def pair_fee_rub(lots: int, paper: Paper) -> float:
    """Комиссия за обе ноги (вход или выход): сбор за лот × лотов × 2 ноги."""
    return paper.taker_fee_rub_per_lot * lots * 2
