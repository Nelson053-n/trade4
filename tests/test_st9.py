"""Тесты ST9 — трендовая корзина: Donchian-пробой, ATR-трейл, реверс, P&L."""
from app.st9.engine import St9Engine, Bar


def _eng(**kw):
    d = dict(secid="USDRUBF", don_enter=5, don_exit=3, atr_mult=3.0,
             atr_period=3, pv=1.0, fee_per_lot=0.0, allow_short=True)
    d.update(kw)
    return St9Engine(**d)


def _feed_flat(e, n, px=100.0):
    """n одинаковых баров (тихий рынок) для прогрева окон."""
    for i in range(n):
        e.step(Bar(ts=i, o=px, h=px + 1, l=px - 1, c=px), lots_for_entry=1)


def test_breakout_long():
    """Пробой максимума входного окна → сигнал open long."""
    e = _eng()
    _feed_flat(e, 8)
    sig = e.step(Bar(ts=9, o=100, h=106, l=100, c=105), lots_for_entry=2)
    assert sig and sig["act"] == "open" and sig["new_side"] == "long"
    assert sig["lots"] == 2
    e.open("long", sig["px"], 2, 9, sig["atr"])
    assert e.position.side == "long" and e.position.trail < 105


def test_breakout_short():
    """Пробой минимума → open short."""
    e = _eng()
    _feed_flat(e, 8)
    sig = e.step(Bar(ts=9, o=100, h=100, l=94, c=95), lots_for_entry=1)
    assert sig and sig["act"] == "open" and sig["new_side"] == "short"


def test_no_short_when_disabled():
    e = _eng(allow_short=False)
    _feed_flat(e, 8)
    assert e.step(Bar(ts=9, o=100, h=100, l=94, c=95), lots_for_entry=1) is None


def test_trail_exit_long():
    """ATR-трейл: цена упала до трейла → close (не реверс, если нет противопробоя).
    atr_mult=1 — узкий трейл срабатывает ВЫШЕ противопробойных уровней."""
    e = _eng(atr_mult=1.0)
    _feed_flat(e, 8)
    sig = e.step(Bar(ts=9, o=100, h=106, l=100, c=105), lots_for_entry=1)
    e.open("long", 105, 1, 9, sig["atr"])
    # рост — трейл подтягивается
    e.step(Bar(ts=10, o=105, h=110, l=105, c=109), lots_for_entry=1)
    t1 = e.position.trail
    # падение к трейлу, но выше lo входного окна → close, не реверс
    sig2 = e.step(Bar(ts=11, o=109, h=109, l=t1 - 1, c=t1 - 0.5), lots_for_entry=1)
    assert sig2 and sig2["act"] == "close" and sig2["reason"] == "trail"
    tr = e.close(sig2["px"], 11, sig2["reason"])
    assert tr.side == "long"


def test_reverse_on_counter_breakout():
    """Противопробой входного окна → реверс лонг→шорт."""
    e = _eng()
    _feed_flat(e, 8)
    sig = e.step(Bar(ts=9, o=100, h=106, l=100, c=105), lots_for_entry=1)
    e.open("long", 105, 1, 9, sig["atr"])
    # обвал ниже минимума входного окна (99-х уровней)
    sig2 = e.step(Bar(ts=10, o=105, h=105, l=90, c=91), lots_for_entry=1)
    assert sig2 and sig2["act"] == "reverse" and sig2["new_side"] == "short"


def test_pnl_long_short():
    """P&L: лонг зарабатывает на росте, шорт на падении; комиссии round-trip."""
    e = _eng(fee_per_lot=2.0, pv=10.0, fee_pct_notional=0.0)
    e.open("long", 100.0, 3, 1, atr=1.0)
    tr = e.close(104.0, 2, "trail")
    assert abs(tr.gross_pnl_rub - 4 * 3 * 10) < 0.01      # +120
    assert abs(tr.fees_rub - 12.0) < 0.01                 # 3лота×2₽×2стороны
    assert abs(tr.net_pnl_rub - 108.0) < 0.01
    e.open("short", 100.0, 2, 3, atr=1.0)
    tr2 = e.close(97.0, 4, "reverse")
    assert abs(tr2.gross_pnl_rub - 3 * 2 * 10) < 0.01     # +60


def test_slip_rub_rejects_wrong_unit_fills():
    """Инцидент 30.07: filled-цена в РУБЛЁВОЙ СУММЕ (79310 при цене бара 79.06) дала
    фиктивные +739010₽ «выигрыша на исполнении». Гейт 20% → замер считается неизмеренным."""
    from app.st9.service import St9Session
    from app.st9.engine import St9Trade

    def _tr(entry, exit_, lots, side="long"):
        return St9Trade(secid="X", side=side, entry=entry, exit=exit_, lots=lots,
                        entry_ts=1, exit_ts=2, gross_pnl_rub=0.0, fees_rub=0.0,
                        net_pnl_rub=0.0, reason="flat_all")

    # реальные числа инцидента: USDRUBF ×1000 и IMOEXF ×10
    assert St9Session._slip_rub(_tr(79.06, 80.05, 1), 79310.0, 80050.0, 1000.0) is None
    assert St9Session._slip_rub(_tr(2214.0, 2253.5, 8), 22146.25, 22535.0, 10.0) is None
    # честный филл рядом с ценой бара — считается
    got = St9Session._slip_rub(_tr(10293.7, 10429.7, 17), 10340.6, 10428.2, 1.0)
    assert got is not None and got < 0        # исполнились хуже модели


def test_slippage_summary_ignores_wrong_unit_records():
    """Сводка не суммирует фиктивные записи (иначе avg +191436₽ как на проде 30.07)."""
    from app.st9.service import St9Session
    s = St9Session.__new__(St9Session)
    s.trades = [
        # фикция: филл ×1000 от цены бара
        {"entry": 79.06, "exit": 80.05, "entry_fill": 79310.0, "exit_fill": 80050.0,
         "slip_rub": 739010.0},
        # честная запись
        {"entry": 10293.7, "exit": 10429.7, "entry_fill": 10340.6, "exit_fill": 10428.2,
         "slip_rub": -823.0},
        # сделка без замера
        {"entry": 100.0, "exit": 101.0, "slip_rub": None},
    ]
    r = s._slippage_summary()
    assert r["measured"] == 1 and r["trades"] == 3
    assert r["total_rub"] == -823.0 and r["worst_rub"] == -823.0


def test_fee_is_pct_of_notional():
    """Комиссия — 0.05% нотионала (px×pv×лоты) за сторону, считается по цене КАЖДОЙ
    стороны отдельно (замер по счёту 30.07, 292 сделки: ровно 0.05000% на всех осях)."""
    e = _eng(pv=10.0, fee_per_lot=0.0, fee_pct_notional=0.05)
    e.open("long", 2000.0, 4, 1, atr=1.0)
    tr = e.close(2500.0, 2, "trail")
    fee_in = 2000.0 * 10 * 4 * 0.0005      # 40.0
    fee_out = 2500.0 * 10 * 4 * 0.0005     # 50.0
    assert abs(tr.fees_rub - (fee_in + fee_out)) < 0.01
    # пропорциональна цене: выход дороже входа → комиссия выхода больше
    assert fee_out > fee_in


def test_fee_matches_real_broker_charges():
    """Формула воспроизводит фактические списания со счёта (30.07): 0.05% нотионала
    даёт 5.08 ₽/лот GLDRUBF, 11.07 ₽/лот IMOEXF, 39.19 ₽/лот USDRUBF."""
    for pv, px, expect in ((1.0, 10163.0, 5.081), (10.0, 2214.0, 11.07), (1000.0, 78.4, 39.2)):
        e = _eng(pv=pv, fee_per_lot=0.0, fee_pct_notional=0.05)
        assert abs(e._fee(px, 1) - expect) < 0.02, f"pv={pv}"


def test_fee_uses_absolute_price():
    """Отрицательная цена (битые данные) не даёт отрицательную комиссию."""
    e = _eng(pv=1.0, fee_per_lot=0.0, fee_pct_notional=0.05)
    assert e._fee(-100.0, 2) > 0


def test_no_signal_until_warmup():
    """До прогрева окон сигналов нет."""
    e = _eng()
    for i in range(4):
        assert e.step(Bar(ts=i, o=100, h=120, l=80, c=110), lots_for_entry=1) is None


def test_trail_protects_after_restart():
    """После рестарта (бары пусты, окна не прогреты) открытая позиция ЗАЩИЩЕНА трейлом.
    Дыра ревизии 09.07: старый step выходил по None-индикаторам до проверки трейла."""
    e = _eng()
    # имитация рестарта: позиция восстановлена из session, баров нет
    e.open("long", 100.0, 1, 1, atr=2.0)   # трейл = 100 − 3×2 = 94
    assert len(e.bars) == 0
    # первый же бар после рестарта пробивает трейл — выход обязан сработать
    sig = e.step(Bar(ts=2, o=95, h=95, l=90, c=92), lots_for_entry=1)
    assert sig is not None and sig["act"] == "close" and sig["reason"] == "trail"


# ==================== регрессии ревизии 11.07 (сервисный слой) ====================

def test_bar_is_closed_rejects_forming():
    """Закрытость бара = истёкший ПЕРИОД (begin+interval), а не поле end ISS:
    ISS пишет в end время последней сделки — формирующийся бар проходил старый фильтр."""
    from app.st9.service import bar_is_closed
    now = 10_000 * 60_000
    assert bar_is_closed(now - 60 * 60_000, 60, now)            # час истёк → закрыт
    assert not bar_is_closed(now - 30 * 60_000, 60, now)        # полчаса → формируется
    assert not bar_is_closed(now - 600 * 60_000, 1440, now)     # дневной, 10ч → формируется
    assert bar_is_closed(now - 1440 * 60_000, 1440, now)        # сутки истекли → закрыт


def test_iss_candles_drops_forming_bar(monkeypatch):
    """iss_candles отбрасывает формирующийся бар даже когда его end в прошлом."""
    import app.st9.service as svc
    from datetime import datetime, timezone, timedelta
    now_msk = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=3)
    forming_begin = now_msk.replace(minute=0, second=0, microsecond=0)  # бар ТЕКУЩЕГО часа
    closed_begin = forming_begin - timedelta(hours=1)
    def _fake_iss(url):
        return {"candles": {"columns": ["begin", "end", "open", "high", "low", "close"],
                "data": [
                    [closed_begin.isoformat(sep=" "), "x", 100, 101, 99, 100],
                    # end формирующегося = «последняя сделка» (в прошлом!) — ловушка ISS
                    [forming_begin.isoformat(sep=" "), "x", 100, 105, 99, 104],
                ]}}
    monkeypatch.setattr(svc, "_iss", _fake_iss)
    bars = svc.iss_candles("USDRUBF", "2026-07-01", 60)
    assert len(bars) == 1 and bars[0].c == 100


def _quarterly_session(monkeypatch):
    """Сессия paper с квартальной осью и замоканными pv/свечами."""
    import app.st9.service as svc
    from app.st9.config import St9InstrumentCfg
    s = svc.St9Session()
    s.cfg.mode = "paper"
    icfg = St9InstrumentCfg(secid="GAZR", quarterly=True, interval_min=1440,
                            entry_notional_rub=100_000.0)
    for sec in ("GAZR", "GZU6", "GZZ6"):
        s._pv_cache[sec] = 1.0
    monkeypatch.setattr(svc, "iss_candles",
                        lambda sec, frm, iv=60: [Bar(ts=1, o=50_000, h=50_500,
                                                     l=49_500, c=50_000)])
    return s, icfg


def test_roll_keeps_position_and_bar_marker(monkeypatch):
    """РОЛЛ: позиция переоткрывается на новом контракте и ПЕРЕЖИВАЕТ следующий тик —
    last_bar_ts не сбрасывается (прежний pop уводил в «первый прогрев», который стирал
    позицию; лоты оставались на счёте бесхозными — критический баг ревизии 11.07)."""
    s, icfg = _quarterly_session(monkeypatch)
    eng = s._engine(icfg)
    eng.open("long", 48_000.0, 2, 1, atr=500.0)
    s.contracts["GAZR"] = "GZU6"
    s._last_bar_ts["GAZR"] = 777
    s._roll(eng, icfg, "GZU6", "GZZ6")
    assert eng.position is not None and eng.position.side == "long"
    assert s._last_bar_ts.get("GAZR") == 777          # маркер жив → тик пойдёт в бэкфилл
    assert s.contracts["GAZR"] == "GZZ6"
    assert s.trades and s.trades[-1]["reason"] == "roll"


def test_roll_trail_offset_from_price_not_entry(monkeypatch):
    """Перенос трейла при ролле — отступ от ТЕКУЩЕЙ цены (от entry ослаблял защиту
    прибыльной позиции: entry 40к/цена 50к/трейл 48к давал бы новый трейл ~40к)."""
    s, icfg = _quarterly_session(monkeypatch)
    eng = s._engine(icfg)
    eng.open("long", 40_000.0, 2, 1, atr=500.0)
    eng.position.trail = 48_000.0                      # трейл подтянут к цене (профит)
    s.contracts["GAZR"] = "GZU6"
    s._roll(eng, icfg, "GZU6", "GZZ6")
    # old_px = new_px = 50 000 (мок) → отступ 4% от цены → новый трейл = 48 000, не 40 000
    assert abs(eng.position.trail - 48_000.0) < 1.0


def test_open_uses_actually_filled_lots(monkeypatch):
    """Частичный филл входа: позиция движка = реально налитые лоты (не запрошенные)."""
    import app.st9.service as svc
    from app.st9.config import St9InstrumentCfg
    s = svc.St9Session()
    s.cfg.mode = "paper"
    icfg = St9InstrumentCfg(secid="USDRUBF")
    s._pv_cache["USDRUBF"] = 1000.0
    eng = s._engine(icfg)
    monkeypatch.setattr(s, "_order", lambda sec, lots, d, ref_px=0.0: 1)   # налил только 1
    s._apply_signal(eng, {"act": "open", "new_side": "long", "px": 80.0, "atr": 0.5}, icfg)
    assert eng.position is not None and eng.position.lots == 1


def test_partial_close_keeps_remainder(monkeypatch):
    """Частичное закрытие: движок ведёт остаток (трейл защищает), сделка не фиксируется."""
    import app.st9.service as svc
    from app.st9.config import St9InstrumentCfg
    s = svc.St9Session()
    s.cfg.mode = "paper"
    icfg = St9InstrumentCfg(secid="USDRUBF")
    s._pv_cache["USDRUBF"] = 1000.0
    eng = s._engine(icfg)
    eng.open("long", 80.0, 5, 1, atr=0.5)
    calls = iter([2, 1])                               # первая попытка 2, добивка 1 → 3 из 5
    monkeypatch.setattr(s, "_order", lambda sec, lots, d, ref_px=0.0: next(calls, 0))
    s._apply_signal(eng, {"act": "close", "px": 81.0, "reason": "trail"}, icfg)
    assert eng.position is not None and eng.position.lots == 2
    assert not s.trades


def test_engine_paused_when_pv_unavailable(monkeypatch):
    """pv недоступен (сбой ISS) → движок не создаётся, ось на паузе (прежний fallback
    pv=1.0 давал сайзинг ×1000 на USDRUBF)."""
    import app.st9.service as svc
    from app.st9.config import St9InstrumentCfg
    s = svc.St9Session()
    monkeypatch.setattr(s, "_pv", lambda sec: None)
    assert s._engine(St9InstrumentCfg(secid="USDRUBF")) is None
    assert "USDRUBF" not in s.engines


# ==================== боевой контур tbank_real (двойной гейт) ====================

def test_st9_real_order_blocked_when_not_armed(monkeypatch):
    """real без взвода: ордер не уходит, filled=0 (гейт на КАЖДЫЙ ордер)."""
    import app.st9.service as svc
    from app.st4 import tbank_live as live, tbank_sandbox as sb
    s = svc.St9Session()
    s.cfg.mode = "tbank_real"; s.cfg.account_id = "real-acc"
    monkeypatch.setattr(sb, "find_future", lambda sec: {"uid": "u1"})
    monkeypatch.setattr(live, "post_order",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("не должен уйти")))
    assert s._order("USDRUBF", 2, "BUY", ref_px=80.0) == 0


def test_st9_real_order_armed_goes_live(monkeypatch):
    """Взведённый real после cooldown: 1-лотовые ордера идут в боевой API, sha256-id."""
    import time
    import app.st9.service as svc
    from app.st4 import tbank_live as live, tbank_sandbox as sb
    s = svc.St9Session()
    s.cfg.mode = "tbank_real"; s.cfg.account_id = "real-acc"
    s.state["real_trading_armed"] = True
    s.state["session_started"] = time.time() - 700
    calls = []
    monkeypatch.setattr(sb, "find_future", lambda sec: {"uid": "u1"})
    monkeypatch.setattr(sb, "last_price", lambda uid: 80.0)
    monkeypatch.setattr(live, "post_order",
                        lambda acc, uid, lots, d, oid, **kw:
                        calls.append(oid) or {"lotsExecuted": 1})
    monkeypatch.setattr(sb, "post_order",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("sandbox path!")))
    assert s._order("USDRUBF", 3, "BUY", ref_px=80.0) == 3
    assert len(calls) == 3 and all(len(o) == 32 and "-" not in o for o in calls)
    assert len(set(calls)) == 3                        # id уникальны (дискриминатор i)


def test_st9_real_price_sanity_blocks(monkeypatch):
    """Рынок уехал >5% от сигнальной цены → боевой ордер отменён (filled=0)."""
    import time
    import app.st9.service as svc
    from app.st4 import tbank_live as live, tbank_sandbox as sb
    s = svc.St9Session()
    s.cfg.mode = "tbank_real"; s.cfg.account_id = "real-acc"
    s.state["real_trading_armed"] = True
    s.state["session_started"] = time.time() - 700
    monkeypatch.setattr(sb, "find_future", lambda sec: {"uid": "u1"})
    monkeypatch.setattr(sb, "last_price", lambda uid: 90.0)          # +12.5% от ref 80
    monkeypatch.setattr(live, "post_order",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("не должен уйти")))
    assert s._order("USDRUBF", 1, "BUY", ref_px=80.0) == 0


def test_st9_entry_lots_real_cap():
    """В tbank_real нотионал оси режется потолком real_max_notional_rub."""
    from app.st9.service import St9Session
    from app.st9.config import St9InstrumentCfg
    s = St9Session()
    icfg = St9InstrumentCfg(secid="GLDRUBF", entry_notional_rub=500_000.0)
    s.cfg.strategy.real_max_notional_rub = 100_000.0
    s.cfg.mode = "tbank_real"
    assert s._entry_lots(icfg, 9_000.0, 1.0) == 11     # 100к/9к, не 500к/9к (55)
    s.cfg.mode = "tbank_sandbox"
    assert s._entry_lots(icfg, 9_000.0, 1.0) == 55


def test_entry_lots_rejects_bad_price():
    """Битая цена (px<=0 / pv<=0) → 0 лотов (отказ), не 1 вслепую (иначе обход sanity)."""
    from app.st9.service import St9Session
    s = St9Session()
    icfg = s.cfg.instruments[0]
    assert s._entry_lots(icfg, 0, 1000) == 0       # px=0 → отказ
    assert s._entry_lots(icfg, 100, 0) == 0        # pv=0 → отказ
    assert s._entry_lots(icfg, 100, 1) > 0         # норма → лоты есть


def test_watchdog_predicate():
    """Watchdog-предикат: рестарт только когда live + застой > порога + биржа открыта."""
    import time
    from app.st9.service import St9Session
    s = St9Session()
    now = time.monotonic()
    assert s._watchdog_should_restart(now) is False        # не live
    s.state["live"] = True
    s._live_hb = 0
    assert s._watchdog_should_restart(now) is False        # ещё не было прохода
    s._live_hb = now                                        # свежий проход
    assert s._watchdog_should_restart(now) is False        # не завис


def test_watchdog_stale_triggers_in_market_hours():
    """Застой > порога в торговое время → рестарт (проверяем через прямой ts буднего дня)."""
    import time
    import datetime
    from app.st9.service import St9Session
    s = St9Session()
    s.state["live"] = True
    s._live_hb = time.monotonic() - 40 * 60      # завис 40 мин (порог 25)
    # будний день, 12:00 МСК = основная сессия FORTS (live)
    monday_noon = datetime.datetime(2026, 7, 13, 9, 0, tzinfo=datetime.timezone.utc)  # 12:00 МСК
    ts = monday_noon.timestamp()
    assert s._watchdog_should_restart(time.monotonic(), ts_sec=ts) is True
    # тот же застой ночью воскресенья (выходная сессия 10:00–19:00 уже закрыта) → не рестарт
    sunday_night = datetime.datetime(2026, 7, 12, 18, 0, tzinfo=datetime.timezone.utc)  # 21:00 МСК
    assert s._watchdog_should_restart(time.monotonic(), ts_sec=sunday_night.timestamp()) is False


def test_sizing_by_capital_pct():
    """Сайзинг go_target_pct: нотионал от % капитала на число осей (плечо). 0 = старый режим."""
    from app.st9.service import St9Session
    s = St9Session()
    s.capital_rub = 500_000
    icfg = s.cfg.instruments[0]
    # выкл → по entry_notional_rub (100к / цена)
    s.cfg.strategy.go_target_pct = 0.0
    assert s._entry_lots(icfg, 77, 1000) == 1
    # 15% капитала на N осей реестра, go_frac 0.044
    s.cfg.strategy.go_target_pct = 15.0
    lots = s._entry_lots(icfg, 77, 1000)
    notional = lots * 77 * 1000
    expected = 500_000 * 0.15 / len(s.cfg.instruments) / 0.044
    assert abs(notional - expected) / expected < 0.15   # округление лотов


def test_capital_dd_guard():
    """Стоп просадки капитала: пик отслеживается, при пробое порога 2 тика подряд — flat + блок."""
    from app.st9.service import St9Session
    s = St9Session()
    s.cfg.strategy.capital_dd_stop_pct = 15.0
    s.cfg.trading_enabled = True
    s.capital_rub = 500_000
    s._capital_dd_guard()
    assert s._capital_peak == 500_000 and s._dd_halted is False
    s.capital_rub = 450_000        # DD 10% < 15% — не срабатывает
    s._capital_dd_guard()
    assert s._dd_halted is False and s.cfg.trading_enabled is True
    s.capital_rub = 420_000        # DD 16% > 15% — 1-й тик: ждём подтверждения
    s._capital_dd_guard()
    assert s._dd_halted is False   # ещё не сработал (1/2)
    s._capital_dd_guard()          # 2-й тик подряд — срабатывает
    assert s._dd_halted is True and s.cfg.trading_enabled is False


def test_capital_dd_guard_ignores_anomaly_spike():
    """Пик НЕ подтягивается аномальным выбросом капитала (защита стопа)."""
    from app.st9.service import St9Session
    s = St9Session()
    s.cfg.strategy.capital_dd_stop_pct = 15.0
    s.capital_rub = 500_000
    s._capital_dd_guard()
    assert s._capital_peak == 500_000
    s.capital_rub = 700_000        # +40% скачок (аномалия, сбой API)
    s._capital_dd_guard()
    assert s._capital_peak == 500_000   # пик НЕ обновлён


def test_capital_dd_guard_single_bad_read_no_flat():
    """Единичное битое чтение вниз (1 тик) НЕ закрывает позиции — нужно 2 подтверждения."""
    from app.st9.service import St9Session
    s = St9Session()
    s.cfg.strategy.capital_dd_stop_pct = 15.0
    s.cfg.trading_enabled = True
    s.capital_rub = 500_000
    s._capital_dd_guard()
    s.capital_rub = 400_000        # −20% (битое чтение)
    s._capital_dd_guard()
    assert s._dd_halted is False   # 1 тик — не флэтим
    s.capital_rub = 500_000        # вернулось (было битое)
    s._capital_dd_guard()
    assert s._dd_halted is False and s._dd_breach_count == 0   # счётчик сброшен


def test_capital_dd_guard_off_by_default():
    """Guard выключен при capital_dd_stop_pct=0 (не мешает старому режиму)."""
    from app.st9.service import St9Session
    s = St9Session()
    s.cfg.strategy.capital_dd_stop_pct = 0.0
    s.cfg.trading_enabled = True
    s.capital_rub = 100_000
    s._capital_dd_guard()
    s.capital_rub = 10_000         # −90%, но guard выключен
    s._capital_dd_guard()
    assert s._dd_halted is False and s.cfg.trading_enabled is True


def test_update_strategy_leverage():
    """update_strategy включает плечо + стоп, инициализирует пик от честного капитала."""
    from app.st9.service import St9Session
    s = St9Session()
    s.capital_sizing_rub = 500_000
    r = s.update_strategy({"go_target_pct": 15, "capital_dd_stop_pct": 15})
    assert r["go_target_pct"] == 15 and r["capital_dd_stop_pct"] == 15
    assert s._capital_peak == 500_000          # пик от честного капитала
    # сайзинг теперь от честного капитала (не totalAmountPortfolio)
    icfg = s.cfg.instruments[0]
    n_axes = len(s.cfg.instruments)            # движков нет → делитель = весь реестр
    notional = s._entry_lots(icfg, 77, 1000) * 77 * 1000
    expected = 500_000 * 0.15 / n_axes / 0.044
    assert abs(notional - expected) / expected < 0.15   # в пределах округления лотов


def test_sizing_uses_honest_capital():
    """Сайзинг плеча берёт capital_sizing_rub (money+ГО), НЕ искажённый capital_rub."""
    from app.st9.service import St9Session
    s = St9Session()
    s.cfg.strategy.go_target_pct = 15.0
    s.capital_rub = 578_000            # искажённый totalAmountPortfolio
    s.capital_sizing_rub = 500_000     # честный
    icfg = s.cfg.instruments[0]
    notional = s._entry_lots(icfg, 77, 1000) * 77 * 1000
    # должен считать от 500к, не 578к
    expected = 500_000 * 0.15 / len(s.cfg.instruments) / 0.044
    assert abs(notional - expected) / expected < 0.15   # в пределах округления лотов


def test_update_strategy_validation():
    """Гейт диапазона go_target_pct."""
    import pytest
    from app.st9.service import St9Session
    s = St9Session()
    with pytest.raises(ValueError, match="вне"):
        s.update_strategy({"go_target_pct": 99})


def test_sizing_from_actual_go_per_lot():
    """При плече лоты считаются из ФАКТИЧЕСКОГО ГО на лот (не go_frac 0.044)."""
    from app.st9.service import St9Session
    s = St9Session()
    s.capital_sizing_rub = 500_000
    s.cfg.strategy.go_target_pct = 15.0
    # мок фактического ГО: 11500₽/лот (реальное USDRUBF, не go_frac-оценка)
    s._go_per_lot = lambda sec, side: 11_500.0
    icfg = s.cfg.instruments[0]
    lots = s._entry_lots(icfg, 77, 1000, side="long")
    # целевое ГО на ось = 500к×15%/N осей; лоты = ГО_оси/11500
    expected = int(500_000 * 0.15 / len(s.cfg.instruments) / 11_500.0)
    assert lots == max(1, expected)


def test_sizing_go_fallback_when_api_down():
    """ГО API недоступно (_go_per_lot None) → фолбэк на go_frac-оценку (не падаем)."""
    from app.st9.service import St9Session
    s = St9Session()
    s.capital_sizing_rub = 500_000
    s.cfg.strategy.go_target_pct = 15.0
    s._go_per_lot = lambda sec, side: None      # API недоступно
    icfg = s.cfg.instruments[0]
    lots = s._entry_lots(icfg, 77, 1000, side="long")
    assert lots >= 1                             # фолбэк сработал, вход возможен


def test_lock_reentrant_guard_flat():
    """RLock реентерабелен: guard внутри tick зовёт flat_all — не дедлочит."""
    from app.st9.service import St9Session
    s = St9Session()
    s.cfg.strategy.capital_dd_stop_pct = 15.0
    s.cfg.trading_enabled = True
    s.capital_rub = 500_000
    s._capital_dd_guard()
    s.capital_rub = 400_000
    # два тика подряд → guard вызовет flat_all (берёт тот же RLock) — не должно зависнуть
    with s._lock:                      # имитируем tick, держащий lock
        s._capital_dd_guard()
        s._capital_dd_guard()
    assert s._dd_halted is True         # сработало без дедлока


def test_tick_defers_bars_when_market_closed(monkeypatch):
    """Гейт торгового окна: биржа закрыта (дневной бар GAZR «закрывается» в 00:00) —
    бар откладывается: маркер не двигается, ордера нет; первым тиком после открытия
    тот же бар обрабатывается и вход исполняется (инцидент 18.07: got=0, бар съеден)."""
    import app.st9.service as svc
    from app.st9.config import St9InstrumentCfg
    s = svc.St9Session()
    s.cfg.mode = "paper"
    s.cfg.trading_enabled = True
    icfg = St9InstrumentCfg(secid="USDRUBF", don_enter=5, don_exit=3)
    s.cfg.instruments = [icfg]
    s._pv_cache["USDRUBF"] = 1.0
    eng = s._engine(icfg)
    for i in range(20):                                 # прогрев окон тихим рынком
        eng.step(Bar(ts=i, o=100, h=101, l=99, c=100), lots_for_entry=1)
    s._last_bar_ts["USDRUBF"] = 19                      # не warmup: маркер уже есть
    breakout = Bar(ts=20, o=100, h=106, l=100, c=105)   # пробой канала вверх
    monkeypatch.setattr(svc, "iss_candles", lambda sec, frm, iv=60: [breakout])
    orders = []
    monkeypatch.setattr(s, "_order",
                        lambda sec, lots, d, ref_px=0.0: orders.append((sec, lots)) or lots)
    monkeypatch.setattr(s, "refresh_capital", lambda: None)
    monkeypatch.setattr(s, "_capital_dd_guard", lambda: None)
    monkeypatch.setattr(s, "_forts_open", lambda: False)      # биржа закрыта
    s.tick()
    assert not orders and eng.position is None
    assert s._last_bar_ts["USDRUBF"] == 19              # бар НЕ съеден
    monkeypatch.setattr(s, "_forts_open", lambda: True)       # открытие
    s.tick()
    assert orders and eng.position is not None and eng.position.side == "long"
    assert s._last_bar_ts["USDRUBF"] == 20


def test_tick_defers_roll_when_market_closed(monkeypatch):
    """Ролл при закрытой бирже откладывается (не спамит 400 всю ночь), позиция цела."""
    import app.st9.service as svc
    s, icfg = _quarterly_session(monkeypatch)
    s.cfg.instruments = [icfg]
    eng = s._engine(icfg)
    eng.open("long", 48_000.0, 2, 1, atr=500.0)
    s.contracts["GAZR"] = "GZU6"
    s._last_bar_ts["GAZR"] = 777
    monkeypatch.setattr(s, "_resolve_contract", lambda c: "GZZ6")   # серия сменилась
    monkeypatch.setattr(s, "refresh_capital", lambda: None)
    monkeypatch.setattr(s, "_capital_dd_guard", lambda: None)
    orders = []
    monkeypatch.setattr(s, "_order",
                        lambda sec, lots, d, ref_px=0.0: orders.append(sec) or lots)
    monkeypatch.setattr(s, "_forts_open", lambda: False)
    s.tick()
    assert not orders                                   # ролл не стрелял в закрытую биржу
    assert s.contracts["GAZR"] == "GZU6" and eng.position.lots == 2


def test_imoexf_axis_registered():
    """IMOEXF — третья НЕЗАВИСИМАЯ ось (диверсификатор, ревизия 25.07): часовая,
    не квартальник. Валютные перпы (CNYRUBF/EURRUBF) сознательно НЕ добавлены:
    corr к USDRUBF 0.91/0.84 — та же ставка на рубль, не диверсификация."""
    from app.st9.config import St9Config
    cfg = St9Config()
    ax = {i.secid: i for i in cfg.instruments}
    assert "IMOEXF" in ax
    im = ax["IMOEXF"]
    # окно выхода 35 (ревизия 30.07 на честной комиссии): 45/25 OOS не устоял
    assert (im.don_enter, im.don_exit, im.atr_mult) == (45, 35, 6.0)
    assert im.quarterly is False and im.interval_min == 60


def test_optimized_params_applied():
    """Параметры осей после свипа 25.07 (длинное окно входа + свободный трейл)."""
    from app.st9.config import St9Config
    ax = {i.secid: i for i in St9Config().instruments}
    assert (ax["USDRUBF"].don_enter, ax["USDRUBF"].don_exit,
            ax["USDRUBF"].atr_mult) == (70, 10, 5.0)
    assert (ax["GLDRUBF"].don_enter, ax["GLDRUBF"].don_exit,
            ax["GLDRUBF"].atr_mult) == (45, 16, 5.0)


def test_leverage_divides_by_live_axes_only():
    """Делитель плеча — ТОРГУЮЩИЕ оси: непрогретая ось (нет движка) не должна
    молча забирать долю ГО и резать размер живых осей."""
    from app.st9.service import St9Session
    from app.st9.engine import St9Engine
    s = St9Session()
    s.capital_sizing_rub = 500_000
    s.cfg.strategy.go_target_pct = 15.0
    s._go_per_lot = lambda sec, side: 11_500.0
    icfg = s.cfg.instruments[0]
    # 2 живых движка из полного реестра → делим на 2, а не на len(instruments)
    for sec in ("USDRUBF", "GLDRUBF"):
        s.engines[sec] = St9Engine(sec, 70, 10, 5.0, 14, pv=1000.0)
    lots = s._entry_lots(icfg, 77, 1000, side="long")
    assert lots == int(500_000 * 0.15 / 2 / 11_500.0)


# ============ наблюдаемость издержек исполнения (замер проскальзывания 27.07) ============

def test_slip_rub_sign_and_math():
    """Проскальзывание в ₽: отрицательное = филлы ХУЖЕ цен бара. Знак верен для обеих сторон."""
    from app.st9.service import St9Session
    from app.st9.engine import St9Trade
    mk = lambda side, e, x, lots=2: St9Trade(secid="X", side=side, entry=e, exit=x,
                                             lots=lots, entry_ts=0, exit_ts=1,
                                             gross_pnl_rub=0, fees_rub=0, net_pnl_rub=0,
                                             reason="trail")
    # LONG: купили дороже (77.50 против 77.48) и продали дешевле (77.50 против 77.52)
    tr = mk("long", 77.48, 77.52)
    assert St9Session._slip_rub(tr, 77.50, 77.50, 1000.0) == -80.0     # 2 ноги × 2 лота × 0.02×1000
    # SHORT: продали дешевле и откупили дороже — тоже минус
    tr2 = mk("short", 100.0, 99.0)
    assert St9Session._slip_rub(tr2, 99.9, 99.1, 1.0) == -0.4
    # филлы ровно по бару → 0
    assert St9Session._slip_rub(mk("long", 10.0, 11.0), 10.0, 11.0, 1.0) == 0.0


def test_slip_rub_none_when_fill_unknown():
    """Нет цены филла хотя бы одной ноги → None, НЕ 0: нулём врать нельзя."""
    from app.st9.service import St9Session
    from app.st9.engine import St9Trade
    tr = St9Trade(secid="X", side="long", entry=1.0, exit=2.0, lots=1, entry_ts=0,
                  exit_ts=1, gross_pnl_rub=0, fees_rub=0, net_pnl_rub=0, reason="trail")
    assert St9Session._slip_rub(tr, None, 2.0, 1.0) is None
    assert St9Session._slip_rub(tr, 1.0, None, 1.0) is None
    assert St9Session._slip_rub(tr, 1.0, 2.0, 0) is None      # pv неизвестен


def test_order_averages_fill_price_over_slices(monkeypatch):
    """_order копит executedOrderPrice по 1-лотовым слайсам и делит на лоты:
    executedOrderPrice — СУММА за слайс, не цена контракта (канон st4)."""
    import app.st9.service as svc
    from app.st4 import tbank_sandbox as sb
    s = svc.St9Session()
    s.cfg.mode = "tbank_sandbox"
    s.cfg.account_id = "acc"
    # basicAssetSize=1 → сумма филла равна котировке (GLDRUBF-подобный случай)
    monkeypatch.setattr(sb, "find_future",
                        lambda sec: {"uid": "u1", "basicAssetSize": {"units": "1", "nano": 0}})
    # РАЗНЫЕ цены (units+nano!) — иначе тест зелёный при любой логике усреднения
    resp = iter([
        {"lotsExecuted": "1", "executedOrderPrice": {"units": "78", "nano": 200000000}},
        {"lotsExecuted": "0", "executedOrderPrice": {"units": "0", "nano": 0}},   # реджект
        {"lotsExecuted": "1", "executedOrderPrice": {"units": "78", "nano": 800000000}},
    ])
    monkeypatch.setattr(sb, "post_order", lambda *a, **k: next(resp))
    got = s._order("USDRUBF", 3, "BUY", ref_px=78.0)
    assert got == 2                            # реджект-слайс не налился
    assert abs(s._last_fill_px - 78.5) < 1e-9  # (78.2+78.8)/2, нулевой слайс отброшен


def test_fill_price_divided_by_basic_asset_size(monkeypatch):
    """executedOrderPrice — СУММА В РУБЛЯХ за контракт, не котировка: делим на
    basicAssetSize. Инцидент 28.07: филл IMOEXF (basicAssetSize=10) читался как
    22146 против цены бара 2214 → проскальзывание «19932 тика»."""
    import app.st9.service as svc
    from app.st4 import tbank_sandbox as sb
    s = svc.St9Session()
    s.cfg.mode = "tbank_sandbox"
    s.cfg.account_id = "acc"
    monkeypatch.setattr(sb, "find_future",
                        lambda sec: {"uid": "u1", "basicAssetSize": {"units": "10", "nano": 0}})
    monkeypatch.setattr(sb, "post_order", lambda *a, **k: {
        "lotsExecuted": "1",
        "executedOrderPrice": {"units": "22146", "nano": 250000000}})   # 2214.625 × 10
    assert s._order("IMOEXF", 1, "BUY", ref_px=2214.0) == 1
    assert abs(s._last_fill_px - 2214.625) < 1e-6      # котировка, НЕ рублёвая сумма


def test_order_resets_fill_price_before_early_returns(monkeypatch):
    """_last_fill_px общий на все оси: сброс ДО ранних return, иначе цена филла одной
    оси залипает в сделке другой (tick обходит оси по очереди)."""
    import app.st9.service as svc
    s = svc.St9Session()
    s.cfg.mode = "paper"                       # ранний return до цикла ордеров
    s._last_fill_px = 78.5                     # «осталось» от предыдущей оси
    assert s._order("GLDRUBF", 3, "BUY", ref_px=6000.0) == 3
    assert s._last_fill_px is None             # чужая цена НЕ должна пережить вызов


def test_entry_fill_not_stale_when_price_unknown(monkeypatch):
    """Вход без цены филла СТИРАЕТ прошлую запись оси — иначе протухшее значение
    фабрикует положительное проскальзывание («исполняемся лучше модели»)."""
    import app.st9.service as svc
    from app.st9.engine import Bar
    s = svc.St9Session()
    s.cfg.mode = "paper"                       # paper → _last_fill_px = None
    s._entry_fill_px["USDRUBF"] = 70.0         # протухшее от давно закрытой сделки
    icfg = next(i for i in s.cfg.instruments if i.secid == "USDRUBF")
    eng = svc.St9Engine("USDRUBF", 5, 3, 3.0, 3, pv=1000.0)
    s.engines["USDRUBF"] = eng
    monkeypatch.setattr(s, "save_session", lambda: None)
    sig = {"act": "open", "new_side": "long", "px": 78.0, "atr": 0.5}
    s._apply_signal_locked(eng, sig, icfg)
    assert "USDRUBF" not in s._entry_fill_px   # стёрта, а не унаследована


def test_slippage_summary_counts_only_measured():
    """Сводка издержек считает только сделки с измеренным проскальзыванием."""
    from app.st9.service import St9Session
    s = St9Session()
    s.trades = [{"slip_rub": -100.0}, {"slip_rub": None}, {"slip_rub": -20.0}, {}]
    r = s._slippage_summary()
    assert r["measured"] == 2 and r["trades"] == 4
    assert r["total_rub"] == -120.0 and r["worst_rub"] == -100.0
    assert r["avg_rub"] == -60.0
    assert St9Session()._slippage_summary()["measured"] == 0    # пустой журнал не падает


def test_entry_fill_survives_restart(tmp_path, monkeypatch):
    """Цена филла ВХОДА переживает рестарт — иначе проскальзывание сделки, открытой
    до перезапуска, посчитать уже нечем."""
    import app.st9.service as svc
    s = svc.St9Session()
    s._session_file = tmp_path / "s9.json"
    s._entry_fill_px = {"USDRUBF": 78.25}
    s.save_session()
    s2 = svc.St9Session()
    s2._session_file = tmp_path / "s9.json"
    s2.load_session()
    assert s2._entry_fill_px == {"USDRUBF": 78.25}


def test_implausible_entry_fill_dropped_on_load(tmp_path):
    """Битая цена филла из старого session (рублёвая сумма вместо котировки) не должна
    пережить загрузку — иначе даст фиктивное проскальзывание в миллионы при закрытии."""
    import json
    import app.st9.service as svc
    f = tmp_path / "s9.json"
    f.write_text(json.dumps({
        "entry_fill_px": {"IMOEXF": 22146.25, "GLDRUBF": 10250.9},
        "positions": {"IMOEXF": {"side": "long", "entry": 2214.0, "lots": 8,
                                 "entry_ts": 1, "trail": 2100.0, "fees_rub": 0.0},
                      "GLDRUBF": {"side": "long", "entry": 10224.7, "lots": 16,
                                  "entry_ts": 1, "trail": 10000.0, "fees_rub": 0.0}},
    }, ensure_ascii=False))
    s = svc.St9Session()
    s._session_file = f
    s.load_session()
    assert "IMOEXF" not in s._entry_fill_px       # 22146 против 2214 — ×10, выброшена
    assert s._entry_fill_px["GLDRUBF"] == 10250.9  # правдоподобная — сохранена
