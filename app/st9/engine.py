"""St9Engine — Donchian-пробой + ATR-трейлинг на одном инструменте (60м бары).

Чистая логика без сети: подаёшь закрытые бары (ts, o, h, l, c) через step(),
получаешь действие. Позиция одна, реверс по противоположному пробою (SAR-стиль,
но окна ДЛИННЫЕ — удержание днями, не band). P&L в пунктах × pv × лоты.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import deque


@dataclass
class Bar:
    ts: int          # мс UTC
    o: float
    h: float
    l: float
    c: float


@dataclass
class St9Position:
    side: str        # long | short
    entry: float
    lots: int
    entry_ts: int
    trail: float     # ATR-трейлинг-стоп (двигается только в сторону позиции)
    fees_rub: float = 0.0


@dataclass
class St9Trade:
    secid: str
    side: str
    entry: float
    exit: float
    lots: int
    entry_ts: int
    exit_ts: int
    gross_pnl_rub: float
    fees_rub: float
    net_pnl_rub: float
    reason: str      # reverse | trail | flat


class St9Engine:
    def __init__(self, secid: str, don_enter: int, don_exit: int,
                 atr_mult: float, atr_period: int, pv: float,
                 fee_per_lot: float = 2.0, allow_short: bool = True):
        self.secid = secid
        self.don_enter = don_enter
        self.don_exit = don_exit
        self.atr_mult = atr_mult
        self.atr_period = atr_period
        self.pv = pv
        self.fee_per_lot = fee_per_lot
        self.allow_short = allow_short
        need = max(don_enter, don_exit, atr_period) + 2
        self.bars: deque[Bar] = deque(maxlen=need + 60)
        self.position: St9Position | None = None
        self.trades: list[St9Trade] = []
        self.last_signal: str = ""      # для снапшота/отладки

    # ---------- индикаторы ----------
    def _atr(self) -> float | None:
        n = self.atr_period
        if len(self.bars) < n + 1:
            return None
        trs = []
        b = list(self.bars)
        for i in range(-n, 0):
            hi, lo, pc = b[i].h, b[i].l, b[i - 1].c
            trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
        return sum(trs) / n

    def _donchian(self, n: int, exclude_last: bool = True) -> tuple[float, float] | None:
        """(max_high, min_low) за n баров ДО текущего (no-repaint: пробой прошлых экстремумов)."""
        b = list(self.bars)
        if exclude_last:
            b = b[:-1]
        if len(b) < n:
            return None
        window = b[-n:]
        return max(x.h for x in window), min(x.l for x in window)

    # ---------- основной шаг ----------
    def step(self, bar: Bar, lots_for_entry: int) -> dict | None:
        """Закрытый 60м бар → действие {'act': 'open'|'close'|'reverse', ...} или None.
        Исполнение делает сервис; движок только сигналит и ведёт позицию/трейл."""
        self.bars.append(bar)
        don_in = self._donchian(self.don_enter)
        don_out = self._donchian(self.don_exit)
        atr = self._atr()
        if don_in is None or don_out is None or atr is None:
            return None
        hi_in, lo_in = don_in
        hi_out, lo_out = don_out
        p = self.position
        if p is not None:
            # 1) двигаем трейл в сторону позиции
            if p.side == "long":
                p.trail = max(p.trail, bar.c - self.atr_mult * atr)
                trail_hit = bar.c <= p.trail
                counter = bar.c < lo_out          # противопробой exit-окна
            else:
                p.trail = min(p.trail, bar.c + self.atr_mult * atr)
                trail_hit = bar.c >= p.trail
                counter = bar.c > hi_out
            if trail_hit or counter:
                # реверс только по противопробою ВХОДНОГО окна; трейл — просто выход
                rev = None
                if p.side == "long" and bar.c < lo_in and self.allow_short:
                    rev = "short"
                elif p.side == "short" and bar.c > hi_in:
                    rev = "long"
                reason = "trail" if trail_hit else "reverse"
                self.last_signal = f"exit {p.side} ({reason})"
                return {"act": "reverse" if rev else "close",
                        "close_side": p.side, "new_side": rev, "px": bar.c,
                        "reason": reason, "atr": atr}
            return None
        # FLAT: пробой входного окна
        if bar.c > hi_in:
            self.last_signal = "breakout long"
            return {"act": "open", "new_side": "long", "px": bar.c, "atr": atr,
                    "lots": lots_for_entry}
        if self.allow_short and bar.c < lo_in:
            self.last_signal = "breakout short"
            return {"act": "open", "new_side": "short", "px": bar.c, "atr": atr,
                    "lots": lots_for_entry}
        return None

    # ---------- мутации (вызывает сервис после исполнения) ----------
    def open(self, side: str, px: float, lots: int, ts: int, atr: float) -> None:
        trail = px - self.atr_mult * atr if side == "long" else px + self.atr_mult * atr
        self.position = St9Position(side=side, entry=px, lots=lots, entry_ts=ts,
                                    trail=trail, fees_rub=lots * self.fee_per_lot)

    def close(self, px: float, ts: int, reason: str) -> St9Trade:
        p = self.position
        d = 1 if p.side == "long" else -1
        gross = (px - p.entry) * d * p.lots * self.pv
        fees = p.fees_rub + p.lots * self.fee_per_lot
        tr = St9Trade(secid=self.secid, side=p.side, entry=p.entry, exit=px,
                      lots=p.lots, entry_ts=p.entry_ts, exit_ts=ts,
                      gross_pnl_rub=round(gross, 2), fees_rub=round(fees, 2),
                      net_pnl_rub=round(gross - fees, 2), reason=reason)
        self.trades.append(tr)
        self.position = None
        return tr

    def unrealized_rub(self, px: float) -> float:
        p = self.position
        if p is None:
            return 0.0
        d = 1 if p.side == "long" else -1
        return (px - p.entry) * d * p.lots * self.pv - p.fees_rub
