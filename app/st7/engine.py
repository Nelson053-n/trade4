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

    def entry_notional_rub(self) -> float:
        """Нотионал перп-ноги на входе (база для стоп-лосса в %). 0 если нет позиции."""
        p = self.position
        if p is None:
            return 0.0
        return abs(p.perp_entry * p.perp_lots * self.pv_perp)

    def stop_hit(self, snap: DaySnap) -> bool:
        """Стоп-лосс: unrealized < −stop_loss_pct·нотионал перп-ноги. Защита от
        девальвационного гэпа — срабатывает НЕЗАВИСИМО от фандинга (полухедж режет
        движение вдвое, но не останавливает кровотечение при удержании позиции)."""
        p = self.position
        pct = getattr(self.strat, "stop_loss_pct", 0.0)
        if p is None or pct <= 0:
            return False
        notional = self.entry_notional_rub()
        if notional <= 0:
            return False
        # unrealized по текущему снапу: P&L ног + фандинг − комиссии
        legs = ((snap.quart_settle - p.quart_entry) * p.quart_lots * self.pv_quart
                - (snap.perp_settle - p.perp_entry) * p.perp_lots * self.pv_perp)
        unreal = legs + p.funding_rub - p.fees_rub
        return unreal < -(pct / 100.0) * notional

    def gap_against(self, snap: DaySnap) -> bool:
        """Широкий гэп перпа ВВЕРХ (против шорта) за день > gap_block_pct — блок входа
        (толпа могла развернуться на панике/девальвации, вход в разгар опасен)."""
        pct = getattr(self.strat, "gap_block_pct", 0.0)
        if pct <= 0 or self.last_snap is None or self.last_snap.perp_settle <= 0:
            return False
        chg = (snap.perp_settle - self.last_snap.perp_settle) / self.last_snap.perp_settle * 100
        return chg > pct

    def daily_step(self, snap: DaySnap) -> str:
        """'enter' | 'exit' | 'stop' | 'roll' | 'hold' | 'trap' | 'gap_block' | 'none'.
        Фандинг начисляется в hold. stop — аварийный выход по убытку (защита от гэпа)."""
        prev_snap = self.last_snap
        p = self.position
        if p is not None:
            p.funding_rub += snap.swaprate * self.pv_perp * p.perp_lots
            # ЗАЩИТА: стоп-лосс раньше фандингового выхода — гэп может держать фандинг высоким
            if self.stop_hit(snap):
                self.last_snap = snap; self.last_fund_pp = snap.fund_trail_ann_pp
                return "stop"
            if snap.fund_trail_ann_pp < self.strat.fund_exit_pp:
                self.last_snap = snap; self.last_fund_pp = snap.fund_trail_ann_pp
                return "exit"
            if snap.quart_secid != p.quart_secid:
                self.last_snap = snap; self.last_fund_pp = snap.fund_trail_ann_pp
                return "roll"
            self.last_snap = snap; self.last_fund_pp = snap.fund_trail_ann_pp
            return "hold"
        # нет позиции — вход, но с гейтом гэпа (last_snap ещё = prev до присвоения)
        action = "none"
        if snap.fund_trail_ann_pp > self.strat.fund_enter_pp:
            if abs(snap.basis_ann_pp) > self.strat.basis_sane_pp:
                action = "trap"        # дивидендная аномалия базиса
            elif self.gap_against(snap):
                action = "gap_block"   # широкий гэп против шорта — вход опасен
            else:
                action = "enter"
        self.last_snap = snap
        self.last_fund_pp = snap.fund_trail_ann_pp
        return action

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
