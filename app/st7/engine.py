"""St7Engine — «фандинг-давление»: полухеджированный шорт вечного (дневная гранулярность).

Позиция: ШОРТ perp_lots вечного (2×нотионал-паритет) + ЛОНГ quart_lots квартальника (1×).
Направленная экспозиция = половина шорта; фандинг собирается с ПОЛНОГО шорта. P&L честный
по ногам (Δцены × лоты × пункт-стоимость НОГИ) + ежедневный фандинг − комиссии round-trip.
Сигнал: вход при 3д-фандинге > fund_enter_pp, выход < fund_exit_pp. Переиспользует
DaySnap st6 (тот же рыночный снимок дня).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..st6.engine import DaySnap


@dataclass
class St7Position:
    perp_secid: str
    quart_secid: str
    perp_lots: int                    # ШОРТ (2×паритет)
    quart_lots: int                   # ЛОНГ (1×) — полухедж
    perp_entry: float
    quart_entry: float
    entry_date: str
    entry_fund_pp: float              # фандинг на входе (3д, ann)
    funding_rub: float = 0.0
    fees_rub: float = 0.0
    rolled: int = 0


@dataclass
class St7Trade:
    pair: str
    entry_date: str
    exit_date: str
    entry_fund_pp: float
    exit_fund_pp: float
    perp_lots: int
    quart_lots: int
    legs_pnl_rub: float
    funding_rub: float
    fees_rub: float
    net_pnl_rub: float
    reason: str
    days_held: int = 0
    rolled: int = 0


class St7Engine:
    def __init__(self, pair: str, strat, pv_perp: float, pv_quart: float,
                 perp_lots: int, quart_lots: int):
        self.pair = pair
        self.strat = strat
        self.pv_perp = pv_perp
        self.pv_quart = pv_quart
        self.unit_perp = perp_lots
        self.unit_quart = quart_lots
        self.position: St7Position | None = None
        self.trades: list[St7Trade] = []
        self.last_snap: DaySnap | None = None
        self.last_fund_pp: float | None = None

    def daily_step(self, snap: DaySnap) -> str:
        """'enter' | 'exit' | 'roll' | 'hold' | 'trap' | 'none'. Фандинг начисляется в hold."""
        self.last_snap = snap
        self.last_fund_pp = snap.fund_trail_ann_pp
        p = self.position
        if p is not None:
            p.funding_rub += snap.swaprate * self.pv_perp * p.perp_lots
            if snap.fund_trail_ann_pp < self.strat.fund_exit_pp:
                return "exit"
            if snap.quart_secid != p.quart_secid:
                return "roll"
            return "hold"
        if snap.fund_trail_ann_pp > self.strat.fund_enter_pp:
            if abs(snap.basis_ann_pp) > self.strat.basis_sane_pp:
                return "trap"          # дивидендная аномалия базиса — хедж-нога непредсказуема
            return "enter"
        return "none"

    def confirm_enter(self, snap: DaySnap, perp_fill: float, quart_fill: float,
                      fee_rub: float) -> None:
        units = max(1, int(self.strat.units))
        self.position = St7Position(
            perp_secid="", quart_secid=snap.quart_secid,
            perp_lots=units * self.unit_perp, quart_lots=units * self.unit_quart,
            perp_entry=perp_fill, quart_entry=quart_fill,
            entry_date=snap.date, entry_fund_pp=round(snap.fund_trail_ann_pp, 1),
            fees_rub=fee_rub)

    def confirm_roll(self, snap: DaySnap, old_quart_fill: float, new_quart_fill: float,
                     fee_rub: float) -> None:
        """Ролл хеджа с точным переносом P&L старой ноги (как st6)."""
        p = self.position
        p.quart_entry = new_quart_fill - (old_quart_fill - p.quart_entry)
        p.quart_secid = snap.quart_secid
        p.fees_rub += fee_rub
        p.rolled += 1

    def confirm_exit(self, snap: DaySnap, perp_fill: float, quart_fill: float,
                     fee_rub: float, reason: str = "exit") -> St7Trade:
        p = self.position
        legs = ((quart_fill - p.quart_entry) * p.quart_lots * self.pv_quart
                - (perp_fill - p.perp_entry) * p.perp_lots * self.pv_perp)
        fees = p.fees_rub + fee_rub
        net = legs + p.funding_rub - fees
        from datetime import date as _d
        try:
            days = (_d.fromisoformat(snap.date) - _d.fromisoformat(p.entry_date)).days
        except ValueError:
            days = 0
        tr = St7Trade(pair=self.pair, entry_date=p.entry_date, exit_date=snap.date,
                      entry_fund_pp=p.entry_fund_pp,
                      exit_fund_pp=round(snap.fund_trail_ann_pp, 1),
                      perp_lots=p.perp_lots, quart_lots=p.quart_lots,
                      legs_pnl_rub=round(legs, 2), funding_rub=round(p.funding_rub, 2),
                      fees_rub=round(fees, 2), net_pnl_rub=round(net, 2),
                      reason=reason, days_held=days, rolled=p.rolled)
        self.trades.append(tr)
        self.position = None
        return tr

    def pair_fee(self, perp_lots: int, quart_lots: int) -> float:
        return (perp_lots + quart_lots) * self.strat.fee_per_lot

    def unrealized_rub(self) -> float:
        p, s = self.position, self.last_snap
        if p is None or s is None:
            return 0.0
        legs = ((s.quart_settle - p.quart_entry) * p.quart_lots * self.pv_quart
                - (s.perp_settle - p.perp_entry) * p.perp_lots * self.pv_perp)
        return legs + p.funding_rub - p.fees_rub
