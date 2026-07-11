"""St8Engine — «дивидендный набег»: событийная логика входа/выхода по календарю отсечек.

Чистые функции сигналов (без сети/исполнения) — тестируемы изолированно. Сервис вызывает
signal_for_day() каждый торговый день и получает действие 'enter'|'exit'|'stop'|'hold'|'none'.
P&L считается по ФАКТИЧЕСКИМ ценам исполнения (акция + опц. хедж-нога IMOEXF), учёт round-trip
комиссий. Хедж: одновременный шорт IMOEXF на hedge_ratio×нотионал — убирает рыночную бету
удержания (её вклад в 2022 был −45%).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DivEvent:
    """Одно дивидендное событие для тикера: ex_date (день гэпа) и дивиденд."""
    ticker: str
    ex_date: str          # YYYY-MM-DD — день дивидендного гэпа (registryclosedate − 1 торг.день)
    div: float            # дивиденд на акцию, ₽
    div_yield_pct: float  # дивдоходность к цене cum-day, %


@dataclass
class St8Position:
    ticker: str
    entry_date: str
    ex_date: str                       # плановый выход накануне (ex − exit_offset)
    lots: int
    stock_entry: float                 # цена входа акции
    hedge_lots: int = 0                # шорт IMOEXF-фьючерса (0 если хедж выкл)
    hedge_entry: float = 0.0           # цена входа хедж-фьючерса
    fees_rub: float = 0.0
    div_yield_pct: float = 0.0
    side: str = "long"                 # long = набег до отсечки; short = сдувание после
    instrument: str = ""               # secid фьючерса-исполнителя ("" = сама акция)
    unit_value: float = 0.0            # ₽ за пункт×лот исполнителя (pv фьючерса / лотность
    #                                    акции); 0 = lot_size движка. Хранится В ПОЗИЦИИ:
    #                                    мутация engine.lot_size ломала следующий вход акцией


@dataclass
class St8Trade:
    ticker: str
    entry_date: str
    exit_date: str
    lots: int
    stock_pnl_rub: float               # P&L ноги акции
    hedge_pnl_rub: float               # P&L хедж-ноги IMOEXF (гасит бету)
    fees_rub: float
    net_pnl_rub: float
    reason: str                        # exit | stop | expiry
    days_held: int = 0
    div_yield_pct: float = 0.0
    side: str = "long"
    instrument: str = ""               # чем исполнено (фьючерс или акция)


class St8Engine:
    """Один тикер. Хранит текущую позицию и журнал. Логика — по календарю событий."""

    def __init__(self, ticker: str, strat, lot_size: int = 1, pv_hedge: float = 10.0):
        self.ticker = ticker
        self.strat = strat
        self.lot_size = lot_size            # акций в лоте (для нотионала)
        self.pv_hedge = pv_hedge            # пункт-стоимость IMOEXF (10₽)
        self.position: St8Position | None = None
        self.trades: list[St8Trade] = []

    # ---------- сигналы (чистые) ----------
    def entry_signal(self, day: str, events: list[DivEvent], trading_days: list[str]) -> DivEvent | None:
        """Есть ли сегодня вход: day == (ex − entry_days_before) для какого-то события,
        июль не пропущен, дивдоходность достаточна. trading_days — упорядоченный список
        торговых дней (для отсчёта N дней до ex). Возвращает событие или None."""
        if self.position is not None:
            return None
        s = self.strat
        for ev in events:
            if ev.ticker != self.ticker:
                continue
            if s.skip_july and ev.ex_date[5:7] == "07":
                continue
            if ev.div_yield_pct < s.min_div_yield_pct:
                continue
            # день входа = торговый день за entry_days_before до ex
            if ev.ex_date not in trading_days:
                continue
            ex_i = trading_days.index(ev.ex_date)
            entry_i = ex_i - s.entry_days_before
            if entry_i < 0:
                continue
            if trading_days[entry_i] == day:
                return ev
        return None

    def exit_day(self, trading_days: list[str]) -> str | None:
        """Плановый день выхода ЛОНГА: ex − exit_offset_days (накануне гэпа)."""
        p = self.position
        if p is None or p.side != "long" or p.ex_date not in trading_days:
            return None
        ex_i = trading_days.index(p.ex_date)
        out_i = ex_i - self.strat.exit_offset_days
        if out_i < 0:
            return None
        return trading_days[out_i]

    def short_entry_signal(self, day: str, events: list[DivEvent],
                           trading_days: list[str]) -> DivEvent | None:
        """Шорт-нога «пост-дивидендное сдувание»: вход в день гэпа (day == ex_date),
        на ЗАКРЫТИИ — после гэпа, дивиденд шорт не платит. Июль торгуется (лучший месяц);
        месяцы из short_skip_months (декабрь-ралли, август) — фильтр."""
        if self.position is not None or not getattr(self.strat, "short_enabled", False):
            return None
        s = self.strat
        for ev in events:
            if ev.ticker != self.ticker:
                continue
            if int(ev.ex_date[5:7]) in (s.short_skip_months or []):
                continue
            if ev.div_yield_pct < s.min_div_yield_pct:
                continue
            if ev.ex_date == day:
                return ev
        return None

    def short_exit_day(self, trading_days: list[str]) -> str | None:
        """Плановый выкуп шорта: ex + short_hold_days торговых дней."""
        p = self.position
        if p is None or p.side != "short" or p.ex_date not in trading_days:
            return None
        ex_i = trading_days.index(p.ex_date)
        out_i = ex_i + self.strat.short_hold_days
        if out_i >= len(trading_days):
            return None
        return trading_days[out_i]

    def _unit(self) -> float:
        """₽ за пункт×лот текущей позиции (pv фьючерса или лотность акции)."""
        p = self.position
        return (p.unit_value or self.lot_size) if p else self.lot_size

    def check_stop(self, stock_px: float, hedge_px: float) -> bool:
        """Стоп-лосс: чистый (акция − хедж) убыток позиции > stop_loss_pct нотионала входа."""
        p = self.position
        pct = getattr(self.strat, "stop_loss_pct", 0.0)
        if p is None or pct <= 0:
            return False
        notional = abs(p.stock_entry * p.lots * self._unit())
        if notional <= 0:
            return False
        unreal = self._pnl(stock_px, hedge_px)[2]   # net без комиссий выхода
        return unreal < -(pct / 100.0) * notional

    # ---------- P&L (по фактическим ценам) ----------
    def _pnl(self, stock_exit: float, hedge_exit: float) -> tuple[float, float, float]:
        """(stock_pnl, hedge_pnl, sum) в ₽. long: прибыль при росте; short: при падении."""
        p = self.position
        stock = (stock_exit - p.stock_entry) * p.lots * self._unit()
        if p.side == "short":
            stock = -stock
        hedge = 0.0
        if p.hedge_lots > 0:
            # шорт IMOEXF: прибыль при падении фьючерса
            hedge = -(hedge_exit - p.hedge_entry) * p.hedge_lots * self.pv_hedge
        return stock, hedge, stock + hedge

    def _fee(self, notional: float) -> float:
        return abs(notional) * self.strat.fee_rate

    # ---------- исполнение (мутирует состояние) ----------
    def open(self, day: str, ev: DivEvent, stock_px: float,
             hedge_px: float, hedge_lots: int, side: str = "long",
             instrument: str = "", unit_value: float | None = None,
             lots: int | None = None) -> None:
        """unit_value — ₽ за пункт×лот (пункт-стоимость фьючерса или лотность акции),
        хранится в позиции (lot_size движка НЕ мутируем). lots — фактически налитые лоты
        (комиссия входа считается от них, не от quantity_lots)."""
        s = self.strat
        lots = lots or s.quantity_lots
        unit = float(unit_value or self.lot_size)
        notional = stock_px * lots * unit
        fee = self._fee(notional)
        if hedge_lots > 0:
            fee += self._fee(hedge_px * hedge_lots * self.pv_hedge)
        self.position = St8Position(
            ticker=self.ticker, entry_date=day, ex_date=ev.ex_date, lots=lots,
            stock_entry=stock_px, hedge_lots=hedge_lots, hedge_entry=hedge_px,
            fees_rub=fee, div_yield_pct=ev.div_yield_pct, side=side, instrument=instrument,
            unit_value=unit)

    def close(self, day: str, stock_px: float, hedge_px: float, reason: str) -> St8Trade:
        p = self.position
        stock_pnl, hedge_pnl, _ = self._pnl(stock_px, hedge_px)
        # комиссия выхода: обе ноги
        exit_fee = self._fee(stock_px * p.lots * self._unit())
        if p.hedge_lots > 0:
            exit_fee += self._fee(hedge_px * p.hedge_lots * self.pv_hedge)
        fees = p.fees_rub + exit_fee
        net = stock_pnl + hedge_pnl - fees
        # дни удержания = позиция в календаре; простая разница индексов недоступна тут → 0-заглушка
        tr = St8Trade(ticker=self.ticker, entry_date=p.entry_date, exit_date=day, lots=p.lots,
                      stock_pnl_rub=round(stock_pnl, 2), hedge_pnl_rub=round(hedge_pnl, 2),
                      fees_rub=round(fees, 2), net_pnl_rub=round(net, 2), reason=reason,
                      div_yield_pct=p.div_yield_pct, side=p.side, instrument=p.instrument)
        self.trades.append(tr)
        self.position = None
        return tr

    def unrealized_rub(self, stock_px: float, hedge_px: float) -> float:
        if self.position is None:
            return 0.0
        return self._pnl(stock_px, hedge_px)[2] - self.position.fees_rub
