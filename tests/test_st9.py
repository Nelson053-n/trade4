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
    e = _eng(fee_per_lot=2.0, pv=10.0)
    e.open("long", 100.0, 3, 1, atr=1.0)
    tr = e.close(104.0, 2, "trail")
    assert abs(tr.gross_pnl_rub - 4 * 3 * 10) < 0.01      # +120
    assert abs(tr.fees_rub - 12.0) < 0.01                 # 3лота×2₽×2стороны
    assert abs(tr.net_pnl_rub - 108.0) < 0.01
    e.open("short", 100.0, 2, 3, atr=1.0)
    tr2 = e.close(97.0, 4, "reverse")
    assert abs(tr2.gross_pnl_rub - 3 * 2 * 10) < 0.01     # +60


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
    # тот же застой в выходной → не рестарт (баров нет легитимно)
    sunday_noon = datetime.datetime(2026, 7, 12, 9, 0, tzinfo=datetime.timezone.utc)
    assert s._watchdog_should_restart(time.monotonic(), ts_sec=sunday_noon.timestamp()) is False


def test_sizing_by_capital_pct():
    """Сайзинг go_target_pct: нотионал от % капитала на число осей (плечо). 0 = старый режим."""
    from app.st9.service import St9Session
    s = St9Session()
    s.capital_rub = 500_000
    icfg = s.cfg.instruments[0]
    # выкл → по entry_notional_rub (100к / цена)
    s.cfg.strategy.go_target_pct = 0.0
    assert s._entry_lots(icfg, 77, 1000) == 1
    # 15% капитала на 3 оси, go_frac 0.044 → нотионал ~570к/ось
    s.cfg.strategy.go_target_pct = 15.0
    lots = s._entry_lots(icfg, 77, 1000)
    notional = lots * 77 * 1000
    assert 450_000 < notional < 650_000     # порядок ~570к (округление лотов)


def test_capital_dd_guard():
    """Стоп просадки капитала: пик отслеживается, при пробое порога — flat + блок входов."""
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
    s.capital_rub = 420_000        # DD 16% > 15% — срабатывает
    s._capital_dd_guard()
    assert s._dd_halted is True and s.cfg.trading_enabled is False


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
