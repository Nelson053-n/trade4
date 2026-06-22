"""TradingEngine — конечный автомат st4 (§9.1) + оркестрация.

Принимает закрытые свечи обеих ног → строит спред-бар → гоняет BB → генерирует сигнал
(вход требует approve при ручном режиме, выход/стоп авто) → исполняет пару через
OrderExecutor → ведёт позицию/журнал/P&L → RiskManager. Reconciliation на старте сверяет
сохранённое состояние с «фактическими» позициями (в paper — со своим же снимком).

Поток: candle_ord/candle_pref → spread_bar → step(bar) → StepResult.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from .config import St4Config
from .execution import OrderExecutor, UnwindError, leg_pnl_rub, pair_fee_rub
from .indicators import BollingerBands, SpreadBuilder, VolumeAverage
from .models import (
    BandReading,
    BotState,
    EngineEvent,
    InstrumentSpec,
    LegPosition,
    Position,
    Role,
    Signal,
    SpreadBar,
    Trade,
)
from .risk import RiskManager
from .strategy import entry_signal, exit_signal, in_clearing_window


@dataclass
class StepResult:
    state: BotState
    band: Optional[BandReading] = None
    signal: Signal = Signal.NONE
    trade: Optional[Trade] = None
    awaiting_approval: bool = False
    events: list[EngineEvent] = field(default_factory=list)


class TradingEngine:
    def __init__(self, cfg: St4Config, spec_ord: InstrumentSpec, spec_pref: InstrumentSpec) -> None:
        self.cfg = cfg
        self.spec_ord = spec_ord
        self.spec_pref = spec_pref
        self.bb = BollingerBands(cfg.strategy.sma_period, cfg.strategy.sigma_multiplier,
                                 cfg.strategy.std_mode)
        # средний объём бара спреда (для объёмного фильтра входа) — то же окно, что и BB
        self.volavg = VolumeAverage(cfg.strategy.sma_period)
        self.builder = SpreadBuilder()
        # выбор исполнителя: paper (по умолчанию) или sandbox T-Bank. Sandbox-импорт локальный,
        # чтобы paper-режим и тесты не тащили сетевые зависимости. Sandbox стартует в своём
        # конструкторе (счёт+pay_in) — ошибки ловит service.reset_engine (откат в paper).
        if cfg.connector.mode == "tbank_sandbox":
            from .tinkoff_executor import TinkoffSandboxExecutor
            self.executor = TinkoffSandboxExecutor(cfg.execution, cfg.connector,
                                                   spec_ord, spec_pref)
        else:
            self.executor = OrderExecutor(cfg.execution, cfg.paper, spec_ord, spec_pref)
        self.risk = RiskManager(cfg.risk, cfg.session)

        self.state = BotState.FLAT
        self.position: Optional[Position] = None
        self.trades: list[Trade] = []
        self.balance_rub = cfg.paper.start_balance_rub
        self._prev: Optional[BandReading] = None
        self._bars_held = 0
        self._pending: Optional[tuple[Signal, BandReading]] = None  # ждёт approve (ручной режим)
        self.last_band: Optional[BandReading] = None
        self._last_spread_bar: Optional[SpreadBar] = None
        # «взведён» ли вход. На backfill-replay (исторические бары) sandbox-исполнение
        # бессмысленно — T-Bank исполнит по ТЕКУЩЕЙ цене, не по цене старого бара. Поэтому
        # на replay движок только прогревает BB (входы не открываем), а торгует с первого
        # ЖИВОГО бара. Для paper/синтетики всегда armed (поведение не меняется).
        self._armed = True
        # гейт свежести данных активен только в live (run_live ставит True). В бэктесте/
        # плеере (run_df) бары исторические — по wall-clock они «старые», блокировать нельзя.
        self._check_lag = False

    def arm(self, on: bool) -> None:
        """Разрешить/запретить открытие новых входов (выход открытой позиции — всегда работает)."""
        self._armed = on

    # ---------- подача данных ----------
    def on_candles(self, ts: int, close_ord: float, close_pref: float,
                   vol_ord: float = 0.0, vol_pref: float = 0.0) -> Optional[StepResult]:
        """Добавить закрытые свечи обеих ног за интервал ts; если бар спреда готов — step.

        vol_ord/vol_pref — объёмы ног за бар (0 = неизвестны: старые вызовы/синтетика без
        объёма; объёмный фильтр в этом случае не блокирует вход).
        """
        self.builder.add_ordinary(ts, close_ord, vol_ord)
        bar = self.builder.add_preferred(ts, close_pref, vol_pref)
        if bar is None:
            return None
        return self.step(bar)

    def warmup(self, spreads: list[float]) -> None:
        """Прогрев BB историей спреда (§8.3) — без сигналов и сделок."""
        self.bb.warmup(spreads)

    def run_df(self, df) -> list[StepResult]:
        """Прогнать движок по DataFrame (price_a=SBRF, price_b=SBPR) бар за баром.

        Для бэктеста/плеера/смоук-теста. Возвращает результаты шагов с непустыми событиями
        или сделками (прогрев BB проматывается тихо).
        """
        out: list[StepResult] = []
        for ts, row in df.iterrows():
            res = self.on_candles(int(ts), float(row["price_a"]), float(row["price_b"]))
            if res is not None and (res.events or res.trade or res.awaiting_approval):
                out.append(res)
        return out

    def days_to_expiry(self, ts_ms: int) -> Optional[int]:
        """Дней до ближайшей экспирации ног на момент ts (None — экспирация неизвестна,
        напр. синтетика). Используется гейтами роллировера (§6.4)."""
        exps = [s.expiry for s in (self.spec_ord, self.spec_pref) if s.expiry]
        if not exps:
            return None
        try:
            exp = min(date.fromisoformat(e) for e in exps)
        except ValueError:
            return None
        cur = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date()
        return (exp - cur).days

    # ---------- фильтры входа ----------
    def _volume_ok(self, bar: SpreadBar) -> bool:
        """Объёмный фильтр: пробой принимается, если объём бара ≥ mult·SMA(объёма).

        Выключен при mult=0. При неизвестном объёме (bar.volume==0 — старые данные/синтетика
        без объёма) или ненабранной/нулевой SMA фильтр НЕ блокирует: считаем непройденным
        только реальный объём (>0), который ниже порога.
        """
        mult = self.cfg.strategy.volume_filter_mult
        if mult <= 0 or bar.volume <= 0:
            return True
        sma = getattr(self, "_vol_sma", float("nan"))
        if not (sma > 0):   # NaN или 0 → данных об объёме нет, не блокируем
            return True
        return bar.volume >= mult * sma

    def _data_fresh(self, bar: SpreadBar) -> bool:
        """Гейт свежести: бар не старше max_data_lag_min минут (по wall-clock).

        Активен только в live (self._check_lag=True) — в бэктесте/плеере исторические бары
        по часам «старые», но это нормально. Выключен при max_data_lag_min=0.
        """
        lag_max = self.cfg.strategy.max_data_lag_min
        if not self._check_lag or lag_max <= 0:
            return True
        return (time.time() * 1000 - bar.ts) <= lag_max * 60_000

    # ---------- основной шаг FSM ----------
    def step(self, bar: SpreadBar) -> StepResult:
        self._last_spread_bar = bar
        band = self.bb.update(bar.ts, bar.spread)
        self.last_band = band
        # средний объём — обновляем на КАЖДОМ баре (то же окно, что BB), чтобы фильтр был
        # синхронен с полосами. NaN на прогреве и при нулевых объёмах не мешает (см. гейт ниже).
        self._vol_sma = self.volavg.update(bar.volume)
        events: list[EngineEvent] = []

        if self.state == BotState.HALTED:
            self._prev = band
            return StepResult(state=self.state, band=band, events=events)

        if self.position is not None:
            self._bars_held += 1

        # ---- дневной kill-switch (§11): реализованный + нереализованный убыток ----
        if self.position is not None and self.risk.day_loss_breached(
                bar.ts, self.unrealized_rub()):
            trade = self._close_position(bar, "stop")
            self.risk.halt(f"дневной лимит убытка {self.cfg.risk.max_daily_loss_rub:.0f}₽")
            self.state = BotState.HALTED
            events.append(EngineEvent(bar.ts, "halt",
                          f"дневной лимит: позиция закрыта (net {trade.net_pnl_rub:+.0f}₽), HALTED", {}))
            self._prev = band
            return StepResult(state=self.state, band=band, signal=Signal.EXIT,
                              trade=trade, events=events)

        # ---- роллировер (§6.4): позиция не доживает до экспирации серии ----
        if self.position is not None:
            d2e = self.days_to_expiry(bar.ts)
            if d2e is not None and d2e < self.cfg.instruments.rollover_days_before_expiry:
                trade = self._close_position(bar, "rollover")
                events.append(EngineEvent(bar.ts, "exit",
                              f"роллировер: до экспирации {d2e} дн — закрытие "
                              f"(net {trade.net_pnl_rub:+.0f}₽)", {}))
                self._prev = band
                return StepResult(state=self.state, band=band, signal=Signal.EXIT,
                                  trade=trade, events=events)

        # ---- управление открытой позицией (выход — авто) ----
        if self.position is not None and self._prev is not None and band.is_ready:
            sma_level = (self.position.sma_at_entry
                         if self.cfg.strategy.freeze_sma_on_exit else band.sma)
            crossed = exit_signal(self.state, self._prev, band, sma_level)
            time_stop = (self.cfg.strategy.max_bars_in_trade > 0
                         and self._bars_held >= self.cfg.strategy.max_bars_in_trade)
            # защитный стоп (опционально): спред ушёл против позиции дальше stop_sigma·σ
            ss = self.cfg.strategy.stop_sigma
            sigma_stop = False
            if ss > 0 and band.sigma > 0:
                if self.state == BotState.SHORT_SPREAD:
                    sigma_stop = band.spread >= band.sma + ss * band.sigma
                elif self.state == BotState.LONG_SPREAD:
                    sigma_stop = band.spread <= band.sma - ss * band.sigma
            # тейк-профит (опц.): спред почти вернулся к SMA (в пределах take·σ внутри канала) —
            # фиксируем прибыль раньше полного пересечения. Только в «правильную» сторону:
            # SHORT вошёл сверху → тейк когда спред опустился до SMA+take·σ; LONG — зеркально.
            tp = self.cfg.strategy.take_profit_sigma
            take = False
            if tp > 0 and band.sigma > 0 and not crossed:
                if self.state == BotState.SHORT_SPREAD:
                    take = band.spread <= band.sma + tp * band.sigma
                elif self.state == BotState.LONG_SPREAD:
                    take = band.spread >= band.sma - tp * band.sigma
            if crossed or time_stop or sigma_stop or take:
                reason = ("exit" if crossed else "stop" if sigma_stop
                          else "take" if take else "time_stop")
                # гейт неторгового времени: реальный (sandbox) выход в клиринг/ночь/выходные
                # отклоняется биржей (HTTP400 30079). Не пытаемся слать ордер — откладываем
                # выход до открытия рынка (сигнал сохранится, закроемся на ближайшем баре сессии).
                ex = getattr(self, "executor", None)
                if ex is not None and hasattr(ex, "is_tradable") and not ex.is_tradable():
                    events.append(EngineEvent(bar.ts, "warn",
                                  f"выход ({reason}) отложен: рынок закрыт (неторговое время)", {}))
                    self._prev = band
                    return StepResult(state=self.state, band=band, events=events)
                trade = self._close_position(bar, reason)
                events.append(EngineEvent(bar.ts, "exit",
                              f"выход ({reason}): net {trade.net_pnl_rub:+.0f}₽", {}))
                self._prev = band
                return StepResult(state=self.state, band=band, signal=Signal.EXIT,
                                  trade=trade, events=events)

        # ---- поиск входа (рекомендация; в авто-режиме исполняем сразу) ----
        # на disarmed (backfill-replay в sandbox) входы НЕ открываем — только прогрев BB
        if self.position is None and self._armed and self._prev is not None and band.is_ready:
            sig = entry_signal(self._prev, band, self.cfg.strategy)
            if sig != Signal.NONE:
                # гейт экспирации (§6.4): новые входы запрещены в окне роллировера
                d2e = self.days_to_expiry(bar.ts)
                no_entry = self.cfg.instruments.rollover_no_new_entry_days_before
                # сессионный фильтр (§9.7): исполнение происходит на CLOSE бара
                # (bar.ts — open-время), поэтому окно проверяем по моменту исполнения
                exec_ts = bar.ts + self.cfg.strategy.candle_interval_minutes * 60_000
                if d2e is not None and d2e < no_entry:
                    events.append(EngineEvent(bar.ts, "warn",
                                  f"сигнал пропущен: до экспирации {d2e} дн (< {no_entry})", {}))
                elif in_clearing_window(exec_ts, self.cfg.session):
                    events.append(EngineEvent(bar.ts, "warn",
                                  "сигнал в клиринговом окне — пропуск", {}))
                elif not self._volume_ok(bar):
                    events.append(EngineEvent(bar.ts, "warn",
                                  f"сигнал пропущен: объём {bar.volume:.0f} < "
                                  f"{self.cfg.strategy.volume_filter_mult:g}·SMA("
                                  f"{self._vol_sma:.0f})", {}))
                elif not self._data_fresh(bar):
                    lag = (time.time() * 1000 - bar.ts) / 60_000
                    events.append(EngineEvent(bar.ts, "warn",
                                  f"сигнал пропущен: данные устарели ({lag:.0f} мин > "
                                  f"{self.cfg.strategy.max_data_lag_min:g})", {}))
                else:
                    ok, why = self.risk.can_enter(bar.ts, 1 if self.position else 0)
                    if not ok:
                        events.append(EngineEvent(bar.ts, "warn", f"вход запрещён: {why}", {}))
                    elif self.cfg.auto_approve:
                        res = self._open_position(sig, band, bar, events)
                        self._prev = band
                        return res
                    else:
                        self._pending = (sig, band)
                        self.state = (BotState.ENTERING_SHORT if sig == Signal.SELL
                                      else BotState.ENTERING_LONG)
                        events.append(EngineEvent(bar.ts, "signal",
                                      f"сигнал {sig.value.upper()} — ждёт подтверждения", {}))
                        self._prev = band
                        return StepResult(state=self.state, band=band, signal=sig,
                                          awaiting_approval=True, events=events)

        self._prev = band
        return StepResult(state=self.state, band=band, events=events)

    # ---------- human-in-the-loop ----------
    def approve(self) -> Optional[StepResult]:
        """Оператор подтверждает вход по висящей рекомендации."""
        if self._pending is None or self.position is not None:
            return None
        sig, band = self._pending
        self._pending = None
        bar = self._last_spread_bar
        events: list[EngineEvent] = []
        res = self._open_position(sig, band, bar, events)
        return res

    def reject(self) -> None:
        self._pending = None
        if self.state in (BotState.ENTERING_SHORT, BotState.ENTERING_LONG):
            self.state = BotState.FLAT

    # ---------- исполнение ----------
    def _open_position(self, sig: Signal, band: BandReading, bar: SpreadBar,
                       events: list[EngineEvent]) -> StepResult:
        """Открыть парную позицию через OrderExecutor (атомарность/unwind §10)."""
        self.state = (BotState.ENTERING_SHORT if sig == Signal.SELL else BotState.ENTERING_LONG)
        lots = self.cfg.execution.quantity_lots
        beta = self.cfg.hedge.beta
        # Направление ног. Спред = SBPR − SBRF, значит exposure к спреду = +SBPR − SBRF.
        # ВНИМАНИЕ: §2 ТЗ ("шорт спреда = продать SBRF + купить SBPR") математически
        # неверен — buy SBPR + sell SBRF даёт +exposure (выигрыш при РОСТЕ спреда), а не
        # ставку на падение. Следуем математике, а не букве §2:
        #   SELL-сигнал (пробой ВЕРХней полосы, ждём возврат ВНИЗ → ставка на ПАДЕНИЕ спреда)
        #     → шорт спреда = sell SBPR + buy SBRF.
        #   BUY-сигнал (пробой НИЖней полосы, ставка на РОСТ спреда)
        #     → лонг спреда  = buy SBPR + sell SBRF.
        # Тогда P&L позиции = ±(spread_exit − spread_entry), знак согласован с направлением.
        buy_ord = (sig == Signal.SELL)    # SBRF
        buy_pref = (sig == Signal.BUY)    # SBPR
        # книга: в paper лучший бид/аск = close ± полуширина стакана (конфигурируемая —
        # реальный стакан SBPR шире одного тика, см. paper_book_halfspread_ticks)
        ref_ord, ref_pref = bar.close_ord, bar.close_pref
        hs_o = self.cfg.execution.paper_book_halfspread_ticks * self.spec_ord.tick_size
        hs_p = self.cfg.execution.paper_book_halfspread_ticks * self.spec_pref.tick_size
        book_ord = (ref_ord - hs_o, ref_ord + hs_o)
        book_pref = (ref_pref - hs_p, ref_pref + hs_p)

        try:
            r = self.executor.execute_pair(buy_ord, buy_pref, lots, book_ord, book_pref,
                                            ref_ord, ref_pref)
        except UnwindError as e:
            self.risk.halt(str(e))
            self.state = BotState.HALTED
            events.append(EngineEvent(bar.ts, "halt", f"HALTED: {e}", {}))
            return StepResult(state=self.state, band=band, signal=sig, events=events)

        if not r.ok:
            # вход не состоялся (abort/unwind) — позиции нет, остаёмся FLAT
            self.risk.on_error()
            self.state = BotState.HALTED if self.risk.halted else BotState.FLAT
            events.append(EngineEvent(bar.ts, "warn", r.reason, {}))
            return StepResult(state=self.state, band=band, signal=sig, events=events)

        self.risk.on_success()
        filled = r.fill_ord.lots   # фактически исполненные лоты (равенство ног — гарантия исполнителя)
        fee = pair_fee_rub(filled, self.cfg.paper)
        self.balance_rub -= fee
        new_state = BotState.SHORT_SPREAD if sig == Signal.SELL else BotState.LONG_SPREAD
        slip = abs(r.fill_ord.slippage_ticks) + abs(r.fill_pref.slippage_ticks)
        self.position = Position(
            state=new_state,
            leg_ord=LegPosition(self.spec_ord.code, Role.ORDINARY, r.fill_ord.side, filled,
                                r.fill_ord.avg_price),
            leg_pref=LegPosition(self.spec_pref.code, Role.PREFERRED, r.fill_pref.side, filled,
                                 r.fill_pref.avg_price),
            entry_ts=bar.ts, entry_spread=bar.spread, entry_beta=beta,
            sma_at_entry=band.sma, entry_fee_rub=fee,
        )
        self._entry_slip = slip
        self.state = new_state
        self._bars_held = 0
        events.append(EngineEvent(bar.ts, "position",
                      f"вход {new_state.value}: SBRF {r.fill_ord.side} @ {r.fill_ord.avg_price:.0f}, "
                      f"SBPR {r.fill_pref.side} @ {r.fill_pref.avg_price:.0f}", {}))
        return StepResult(state=self.state, band=band, signal=sig, events=events)

    def _close_position(self, bar: SpreadBar, reason: str) -> Trade:
        """Закрыть позицию по ценам бара, посчитать P&L по тикам, записать сделку."""
        p = self.position
        # выходные цены: спрашиваем исполнителя. Paper вернёт цены бара (узкая книга),
        # sandbox — фактический филл реального обратного ордера. P&L считаем по факту.
        cr = self.executor.close_pair(p, bar.close_ord, bar.close_pref)
        exit_ord, exit_pref = cr.exit_ord, cr.exit_pref
        pnl_ord = leg_pnl_rub(p.leg_ord, exit_ord, self.spec_ord)
        pnl_pref = leg_pnl_rub(p.leg_pref, exit_pref, self.spec_pref)
        gross = pnl_ord + pnl_pref
        lots = p.leg_ord.lots
        exit_fee = pair_fee_rub(lots, self.cfg.paper)
        net = gross - p.entry_fee_rub - exit_fee   # полный результат сделки: обе комиссии
        self.balance_rub += gross - exit_fee       # entry_fee уже списан при открытии

        trade = Trade(
            state=p.state, entry_ts=p.entry_ts, exit_ts=bar.ts,
            entry_spread=p.entry_spread, exit_spread=bar.spread, lots=lots,
            gross_pnl_rub=gross, fees_rub=p.entry_fee_rub + exit_fee, net_pnl_rub=net,
            reason=reason, bars_held=self._bars_held,
            ord_side=p.leg_ord.side, pref_side=p.leg_pref.side,
            ord_entry=p.leg_ord.entry_price, ord_exit=exit_ord,
            pref_entry=p.leg_pref.entry_price, pref_exit=exit_pref,
            slippage_ticks=getattr(self, "_entry_slip", 0.0),
        )
        self.trades.append(trade)
        self.risk.on_trade_closed(net, bar.ts)
        self.position = None
        self.state = BotState.FLAT
        self._bars_held = 0
        return trade

    def flat_all(self, reason: str = "flat_all") -> Optional[Trade]:
        """Паник-закрытие: немедленно закрыть позицию по последнему бару (§11)."""
        self._pending = None
        if self.position is None or self._last_spread_bar is None:
            self.state = BotState.FLAT if self.state != BotState.HALTED else self.state
            return None
        return self._close_position(self._last_spread_bar, reason)

    # ---------- reconciliation (§11) ----------
    def reconcile(self, broker_position: Optional[Position]) -> bool:
        """Сверить состояние с «фактической» позицией брокера. True — согласовано.

        В paper «брокер» — наш же снимок; при расхождении (например, после крэша
        процесса с рассинхроном) переводим в HALTED, чтобы не было двойного входа.
        """
        local = self.position
        same = (local is None) == (broker_position is None)
        if same and local is not None and broker_position is not None:
            same = (local.state == broker_position.state
                    and local.leg_ord.lots == broker_position.leg_ord.lots)
        if not same:
            self.risk.halt("reconciliation: расхождение локального состояния и позиций брокера")
            self.state = BotState.HALTED
            return False
        return True

    # ---------- сводка ----------
    def unrealized_rub(self) -> float:
        if self.position is None or self._last_spread_bar is None:
            return 0.0
        p = self.position
        bar = self._last_spread_bar
        return (leg_pnl_rub(p.leg_ord, bar.close_ord, self.spec_ord)
                + leg_pnl_rub(p.leg_pref, bar.close_pref, self.spec_pref))

    def summary(self) -> dict:
        wins = [t for t in self.trades if t.net_pnl_rub > 0]
        net = sum(t.net_pnl_rub for t in self.trades)
        eq = self.balance_rub + self.unrealized_rub()
        start = self.cfg.paper.start_balance_rub
        return {
            "trades": len(self.trades),
            "win_rate_pct": round(100 * len(wins) / len(self.trades), 1) if self.trades else 0.0,
            "net_pnl_rub": round(net, 0),
            "fees_rub": round(sum(t.fees_rub for t in self.trades), 0),
            "balance_rub": round(self.balance_rub, 0),
            "equity_rub": round(eq, 0),
            "return_pct": round(100 * (eq - start) / start, 3),
            "day_pnl_rub": round(self.risk.day_pnl_rub, 0),
            "stops": sum(1 for t in self.trades if t.reason in ("stop", "time_stop")),
        }
