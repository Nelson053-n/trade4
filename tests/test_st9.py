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
