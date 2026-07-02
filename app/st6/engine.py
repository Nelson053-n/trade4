"""St6Engine — фандинг-арбитраж одной пары «вечный vs квартальный» (дневная гранулярность).

Позиция: ШОРТ perp_lots вечного + ЛОНГ quart_lots квартальника. P&L честный, по ногам:
Δцены × лоты × пункт-стоимость НОГИ (у ног она разная: IMOEXF 10₽/пункт, MX 1₽/пункт) +
ежедневное начисление фандинга (SWAPRATE × pv_perp × perp_lots — шорт получает положительный)
− комиссии round-trip. Сигнал: edge = аннуализ. трейл-фандинг − аннуализ. базис квартальника.

Движок чистый (без I/O): daily_step получает рыночный снимок дня, возвращает действие;
исполнение и подтверждение филлов — уровнем выше (service), фиксация позиции по фактическим
ценам через confirm_*.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class St6Position:
    perp_secid: str
    quart_secid: str
    perp_lots: int                    # лоты вечного (ШОРТ)
    quart_lots: int                   # лоты квартальника (ЛОНГ)
    perp_entry: float                 # цены входа (пункты своих контрактов)
    quart_entry: float
    entry_date: str
    entry_edge_pp: float
    funding_rub: float = 0.0          # накопленный фандинг (₽, + нам)
    fees_rub: float = 0.0             # комиссии входа (+роллов)
    rolled: int = 0                   # сколько раз роллировали квартальную ногу


@dataclass
class St6Trade:
    pair: str
    entry_date: str
    exit_date: str
    entry_edge_pp: float
    exit_edge_pp: float
    perp_lots: int
    quart_lots: int
    legs_pnl_rub: float               # P&L ног по ценам входа/выхода
    funding_rub: float                # собранный фандинг
    fees_rub: float
    net_pnl_rub: float
    reason: str                       # exit | manual | halt
    days_held: int = 0
    rolled: int = 0


@dataclass
class DaySnap:
    """Рыночный снимок дня для daily_step (собирает service из ISS)."""
    date: str
    perp_settle: float
    swaprate: float                   # фандинг дня (единицы цены перпа)
    fund_trail_ann_pp: float          # аннуализ. средний фандинг за trail-окно, % годовых
    quart_secid: str                  # ближняя серия (с учётом roll-порога — может быть следующей)
    quart_settle: float
    basis_ann_pp: float               # аннуализ. базис ближнего квартальника, % годовых


class St6Engine:
    def __init__(self, pair: str, strat, pv_perp: float, pv_quart: float,
                 perp_lots: int, quart_lots: int):
        self.pair = pair
        self.strat = strat
        self.pv_perp = pv_perp
        self.pv_quart = pv_quart
        self.unit_perp = perp_lots    # лотов перпа в юните (нотионал-паритет с квартальником)
        self.unit_quart = quart_lots
        self.position: St6Position | None = None
        self.trades: list[St6Trade] = []
        self.last_edge_pp: float | None = None
        self.last_snap: DaySnap | None = None

    # ---------- сигнал ----------
    def edge_pp(self, snap: DaySnap) -> float:
        return snap.fund_trail_ann_pp - snap.basis_ann_pp

    def daily_step(self, snap: DaySnap) -> str:
        """Обработать день: начислить фандинг, вернуть действие для service:
        'enter' | 'exit' | 'roll' | 'hold' | 'none'. Исполнение подтверждается confirm_*."""
        self.last_snap = snap
        edge = self.edge_pp(snap)
        self.last_edge_pp = edge
        p = self.position
        if p is not None:
            # фандинг дня: шорт перпа получает положительный SWAPRATE
            p.funding_rub += snap.swaprate * self.pv_perp * p.perp_lots
            if edge < self.strat.edge_exit_pp:
                return "exit"
            if snap.quart_secid != p.quart_secid:
                return "roll"          # ближняя серия сменилась (порог ролла) → перекладываем хедж
            return "hold"
        if edge > self.strat.edge_enter_pp:
            if abs(snap.basis_ann_pp) > self.strat.basis_sane_pp:
                return "none"          # дивидендная ловушка: аномальный базис → сигнал фиктивен
            return "enter"
        return "none"

    # ---------- подтверждения исполнения (фактические цены филлов) ----------
    def confirm_enter(self, snap: DaySnap, perp_fill: float, quart_fill: float,
                      fee_rub: float) -> None:
        units = max(1, int(self.strat.units))
        self.position = St6Position(
            perp_secid="", quart_secid=snap.quart_secid,
            perp_lots=units * self.unit_perp, quart_lots=units * self.unit_quart,
            perp_entry=perp_fill, quart_entry=quart_fill,
            entry_date=snap.date, entry_edge_pp=round(self.edge_pp(snap), 2),
            fees_rub=fee_rub)

    def confirm_roll(self, snap: DaySnap, old_quart_fill: float, new_quart_fill: float,
                     fee_rub: float) -> None:
        """Ролл хеджа: закрыт старый квартальник, открыт новый. Реализованный P&L старой
        ноги переносится сдвигом entry новой (entry_new = new_fill − (old_fill − entry_old)) —
        суммарный legs-P&L позиции сохраняется точно, без отдельного поля realized."""
        p = self.position
        p.quart_entry = new_quart_fill - (old_quart_fill - p.quart_entry)
        p.quart_secid = snap.quart_secid
        p.fees_rub += fee_rub
        p.rolled += 1

    def confirm_exit(self, snap: DaySnap, perp_fill: float, quart_fill: float,
                     fee_rub: float, reason: str = "exit") -> St6Trade:
        p = self.position
        legs = ((quart_fill - p.quart_entry) * p.quart_lots * self.pv_quart
                - (perp_fill - p.perp_entry) * p.perp_lots * self.pv_perp)
        fees = p.fees_rub + fee_rub
        net = legs + p.funding_rub - fees
        from datetime import date as _d
        try:
            days = ( _d.fromisoformat(snap.date) - _d.fromisoformat(p.entry_date)).days
        except ValueError:
            days = 0
        tr = St6Trade(pair=self.pair, entry_date=p.entry_date, exit_date=snap.date,
                      entry_edge_pp=p.entry_edge_pp, exit_edge_pp=round(self.edge_pp(snap), 2),
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
