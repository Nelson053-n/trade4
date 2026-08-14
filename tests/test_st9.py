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


def _sess_for_orders(mode="tbank_sandbox"):
    """Голая сессия для тестов исполнения (без сети в конструкторе)."""
    from app.st9.service import St9Session
    from app.st9.config import St9Config
    s = St9Session.__new__(St9Session)
    s.cfg = St9Config()
    s.cfg.mode = mode
    s.cfg.account_id = "acc"
    s.events = []
    s.log_event = lambda k, m: s.events.append(m)
    s._tick_cache = {}
    s._last_fill_px = None
    return s


def test_limit_cap_accounts_for_requested_volume():
    """Потолок берётся с уровня, где НАБИРАЕТСЯ объём заявки, а не с первого уровня:
    иначе на тонком стакане лимит отсекает хвост и мы получаем недолив."""
    from app.st4 import tbank_sandbox as sb
    s = _sess_for_orders()
    s._tick_cache["u"] = 0.5
    book = {"asks": [{"price": 100.0, "qty": 2}, {"price": 100.5, "qty": 3},
                     {"price": 101.0, "qty": 50}], "bids": []}
    old = sb.order_book
    sb.order_book = lambda uid, depth=10: book
    try:
        # 2 лота набираются на первом уровне → потолок 100.0 + 2 тика
        assert s._limit_cap("u", True, 2) == 101.0
        # 6 лотов требуют третьего уровня (2+3+50) → потолок 101.0 + 2 тика
        assert s._limit_cap("u", True, 6) == 102.0
    finally:
        sb.order_book = old


def test_limit_cap_is_marketable_not_passive():
    """Для BUY потолок ВЫШЕ ask (не ниже): пассивная лимитка на пробое не исполнится
    и сигнал будет потерян. Для SELL — ниже bid."""
    from app.st4 import tbank_sandbox as sb
    s = _sess_for_orders()
    s._tick_cache["u"] = 0.1
    book = {"asks": [{"price": 200.0, "qty": 99}], "bids": [{"price": 199.0, "qty": 99}]}
    old = sb.order_book
    sb.order_book = lambda uid, depth=10: book
    try:
        assert s._limit_cap("u", True, 1) > 200.0     # buy платит выше ask
        assert s._limit_cap("u", False, 1) < 199.0    # sell отдаёт ниже bid
    finally:
        sb.order_book = old


def test_limit_cap_none_when_tick_unknown():
    """Неизвестный шаг цены → маркет, а НЕ лимит по некратной цене (биржа отвергнет).
    Ровно эта дыра сделала лимитки st5 фикцией на полгода."""
    from app.st4 import tbank_sandbox as sb
    s = _sess_for_orders()
    old_f, old_ob = sb.future_by_uid, sb.order_book
    sb.future_by_uid = lambda uid: (_ for _ in ()).throw(RuntimeError("нет в справочнике"))
    sb.order_book = lambda uid, depth=10: {"asks": [{"price": 100.0, "qty": 9}], "bids": []}
    try:
        assert s._limit_cap("u", True, 1) is None
    finally:
        sb.future_by_uid, sb.order_book = old_f, old_ob


def test_limit_cap_rounds_to_tick():
    """Лимит-цена кратна шагу: биржа отвергает некратные."""
    from app.st4 import tbank_sandbox as sb
    s = _sess_for_orders()
    s._tick_cache["u"] = 0.25
    old = sb.order_book
    sb.order_book = lambda uid, depth=10: {"asks": [{"price": 10.13, "qty": 9}], "bids": []}
    try:
        cap = s._limit_cap("u", True, 1)
        assert abs(cap / 0.25 - round(cap / 0.25)) < 1e-9, cap
    finally:
        sb.order_book = old


def test_order_sends_single_order_for_all_lots():
    """Весь объём одним ордером, а не N по 1 лоту: 17 лотов = 17 round-trip'ов,
    цена успевает уйти (замер: ~1.1с только на сеть)."""
    from app.st4 import tbank_sandbox as sb
    s = _sess_for_orders()
    sent = []
    old = (sb.find_future, sb.post_order, sb.order_book, sb.future_by_uid)
    sb.find_future = lambda x: {"uid": "u", "basicAssetSize": {"units": "1", "nano": 0}}
    sb.future_by_uid = lambda uid: {"minPriceIncrement": {"units": "0", "nano": 100000000}}
    sb.order_book = lambda uid, depth=10: {"asks": [{"price": 100.0, "qty": 99}],
                                           "bids": [{"price": 99.0, "qty": 99}]}
    def _post(acc, uid, lots, direction, oid, order_type="ORDER_TYPE_MARKET", price=None):
        sent.append({"lots": lots, "type": order_type, "price": price})
        return {"lotsExecuted": str(lots),
                "executedOrderPrice": {"units": str(100 * lots), "nano": 0}}
    sb.post_order = _post
    try:
        got = s._order("GLDRUBF", 17, "BUY", ref_px=100.0)
    finally:
        sb.find_future, sb.post_order, sb.order_book, sb.future_by_uid = old
    assert got == 17
    assert len(sent) == 1, f"ожидали 1 ордер, отправлено {len(sent)}"
    assert sent[0]["lots"] == 17 and sent[0]["type"] == "ORDER_TYPE_LIMIT"


def test_order_tops_up_with_market_when_limit_underfills():
    """Недолив лимитки добирается МАРКЕТОМ: иначе движок считает позицию открытой
    на запрошенный объём, а на счёте меньше — рассинхрон движок↔счёт."""
    from app.st4 import tbank_sandbox as sb
    s = _sess_for_orders()
    sent = []
    old = (sb.find_future, sb.post_order, sb.order_book, sb.future_by_uid)
    sb.find_future = lambda x: {"uid": "u", "basicAssetSize": {"units": "1", "nano": 0}}
    sb.future_by_uid = lambda uid: {"minPriceIncrement": {"units": "0", "nano": 100000000}}
    sb.order_book = lambda uid, depth=10: {"asks": [{"price": 100.0, "qty": 99}],
                                           "bids": [{"price": 99.0, "qty": 99}]}
    def _post(acc, uid, lots, direction, oid, order_type="ORDER_TYPE_MARKET", price=None):
        sent.append({"lots": lots, "type": order_type})
        got = 6 if order_type == "ORDER_TYPE_LIMIT" else lots    # лимит налил не всё
        return {"lotsExecuted": str(got),
                "executedOrderPrice": {"units": str(100 * got), "nano": 0}}
    sb.post_order = _post
    try:
        got = s._order("GLDRUBF", 10, "BUY", ref_px=100.0)
    finally:
        sb.find_future, sb.post_order, sb.order_book, sb.future_by_uid = old
    assert got == 10, f"недолив не добран: {got}"
    assert len(sent) == 2
    assert sent[1]["type"] == "ORDER_TYPE_MARKET" and sent[1]["lots"] == 4


def test_order_falls_back_to_market_when_book_unavailable():
    """Стакан недоступен → маркет. Пропущенный выход опаснее лишнего тика."""
    from app.st4 import tbank_sandbox as sb
    s = _sess_for_orders()
    sent = []
    old = (sb.find_future, sb.post_order, sb.order_book, sb.future_by_uid)
    sb.find_future = lambda x: {"uid": "u", "basicAssetSize": {"units": "1", "nano": 0}}
    sb.future_by_uid = lambda uid: {"minPriceIncrement": {"units": "0", "nano": 100000000}}
    def _boom(uid, depth=10): raise RuntimeError("стакан недоступен")
    sb.order_book = _boom
    def _post(acc, uid, lots, direction, oid, order_type="ORDER_TYPE_MARKET", price=None):
        sent.append(order_type)
        return {"lotsExecuted": str(lots), "executedOrderPrice": {"units": "100", "nano": 0}}
    sb.post_order = _post
    try:
        assert s._order("GLDRUBF", 3, "BUY", ref_px=100.0) == 3
    finally:
        sb.find_future, sb.post_order, sb.order_book, sb.future_by_uid = old
    assert sent == ["ORDER_TYPE_MARKET"]


def test_limit_orders_can_be_disabled():
    """Аварийный тумблер use_limit_orders=False возвращает чистый маркет."""
    from app.st4 import tbank_sandbox as sb
    s = _sess_for_orders()
    s.cfg.strategy.use_limit_orders = False
    sent = []
    old = (sb.find_future, sb.post_order, sb.order_book, sb.future_by_uid)
    sb.find_future = lambda x: {"uid": "u", "basicAssetSize": {"units": "1", "nano": 0}}
    sb.future_by_uid = lambda uid: {"minPriceIncrement": {"units": "0", "nano": 100000000}}
    sb.order_book = lambda uid, depth=10: {"asks": [{"price": 100.0, "qty": 99}], "bids": []}
    def _post(acc, uid, lots, direction, oid, order_type="ORDER_TYPE_MARKET", price=None):
        sent.append(order_type)
        return {"lotsExecuted": str(lots), "executedOrderPrice": {"units": "100", "nano": 0}}
    sb.post_order = _post
    try:
        s._order("GLDRUBF", 5, "BUY", ref_px=100.0)
    finally:
        sb.find_future, sb.post_order, sb.order_book, sb.future_by_uid = old
    assert sent == ["ORDER_TYPE_MARKET"]


def test_poll_seconds_comes_from_code_not_session(tmp_path):
    """poll_seconds берётся ИЗ КОДА, а не из session-файла (ловушка 30.07: смена
    600→60с была инертна на проде — старый session перетирал значение при загрузке).
    Та же защита, что у реестра инструментов."""
    import json
    from app.st9.service import St9Session
    from app.st9.config import St9Config
    s = St9Session.__new__(St9Session)
    s.engines = {}; s.trades = []; s.events = []
    s.state = {"live": False, "live_intent": False}
    s._last_bar_ts = {}; s._pv_cache = {}; s._pv_warned = set()
    s._contract_cache = {}; s._bars_contract = {}; s._pending_positions = {}
    s._deferred_ts = {}; s.contracts = {}; s.axis_overrides = {}
    s.capital_rub = 0.0; s.exec_anchor = None; s.last_tick_ts = 0
    s._entry_fill_px = {}; s._go_lot_cache = {}; s.cfg = St9Config()
    f = tmp_path / "s9.json"
    f.write_text(json.dumps({"config": {"poll_seconds": 600.0, "mode": "paper",
                                        "account_id": "", "trading_enabled": True}}))
    s._session_file = f
    s.load_session()
    assert s.cfg.poll_seconds == St9Config().poll_seconds == 60.0


def test_go_divider_is_registry_not_created_engines():
    """Делитель ГО не должен зависеть от того, сколько движков УЖЕ создано: они
    создаются лениво внутри того же tick(), что сайзит входы (аудит 30.07, HIGH).
    Ось №1 делила на 1 и забирала весь бюджет ГО — ×1.8 на первом тике после рестарта."""
    from app.st9.service import St9Session
    from app.st9.config import St9Config
    s = St9Session.__new__(St9Session)
    s.cfg = St9Config()
    s.cfg.strategy.go_target_pct = 15.0
    s.capital_sizing_rub = 2_000_000.0
    s.capital_rub = 2_000_000.0
    s.events = []
    s.log_event = lambda k, m: None
    s._go_per_lot = lambda sec, side: 15_000.0
    s._trade_secid = lambda icfg: icfg.secid
    n = sum(1 for i in s.cfg.instruments if i.entries_enabled)
    budget = 2_000_000 * 0.15
    total_go = 0.0
    s.engines = {}
    live = [i for i in s.cfg.instruments if i.entries_enabled]   # выведенные не входят
    for icfg in live:
        s.engines[icfg.secid] = object()        # движок появляется ПЕРЕД сайзингом
        total_go += s._entry_lots(icfg, 100.0, 10.0, "long", icfg.secid) * 15_000
    assert total_go <= budget * 1.05, f"ГО {total_go} превысило бюджет {budget}"
    # и первая ось не забирает больше своей доли
    s.engines = {live[0].secid: object()}
    first = s._entry_lots(live[0], 100.0, 10.0, "long", "X") * 15_000
    assert first <= budget / n * 1.05


def test_dd_guard_rearms_when_operator_resumes_entries():
    """Стоп просадки не должен умирать навсегда: оператор возвращает входы штатным
    /st9/control/trading, и защита обязана вернуться (аудит 30.07, HIGH — иначе
    капитал мог падать на 60% при молчащем guard)."""
    from app.st9.service import St9Session
    from app.st9.config import St9Config
    s = St9Session.__new__(St9Session)
    s.cfg = St9Config()
    s.cfg.strategy.capital_dd_stop_pct = 10.0
    s.events = []
    s.log_event = lambda k, m: None
    s.save_session = lambda: None
    flats = []
    s.flat_all = lambda: flats.append(1)
    s._capital_peak = 1_000_000.0
    s._dd_halted = False
    s._dd_breach_count = 0
    s.capital_rub = 0.0
    s.capital_sizing_rub = 880_000.0            # −12% > порога
    s._capital_dd_guard(); s._capital_dd_guard()
    assert s._dd_halted and len(flats) == 1
    s.cfg.trading_enabled = True                # оператор вернул входы
    s.capital_sizing_rub = 700_000.0            # падает дальше от НОВОГО пика
    s._capital_dd_guard(); s._capital_dd_guard()
    assert len(flats) == 2, "guard не сработал повторно — защита мертва"


def test_refresh_capital_skips_update_when_margin_unknown():
    """Недоступное ГО открытой оси НЕ должно занижать честный капитал: при плече это
    фабриковало просадку и закрывало портфель по рынку (аудит 30.07, MED)."""
    from app.st9.service import St9Session
    from app.st9.config import St9Config
    s = St9Session.__new__(St9Session)
    s.cfg = St9Config()
    s.cfg.mode = "tbank_sandbox"
    s.cfg.account_id = "acc"
    s.events = []
    s.log_event = lambda k, m: None
    s.trades = []
    s.exec_anchor = None
    s.contracts = {}
    s.capital_rub = 0.0
    s.capital_sizing_rub = 2_000_000.0          # прошлое честное значение
    class _Eng:
        def __init__(self): self.position = type("P", (), {"side": "long", "lots": 5})()
    s.engines = {"USDRUBF": _Eng()}
    from app.st4 import tbank_sandbox as sb
    old = (sb.portfolio, sb.free_money_rub, sb.find_future, sb.futures_margin)
    sb.portfolio = lambda a: {"totalAmountPortfolio": {"units": "2000000", "nano": 0}}
    sb.free_money_rub = lambda a: 1_500_000.0
    sb.find_future = lambda s_: {"uid": "u"}
    def _boom(uid): raise RuntimeError("API down")
    sb.futures_margin = _boom
    try:
        s.refresh_capital()
    finally:
        sb.portfolio, sb.free_money_rub, sb.find_future, sb.futures_margin = old
    assert s.capital_sizing_rub == 2_000_000.0, "капитал занижен при недоступном ГО"


def test_go_per_lot_not_poisoned_by_bad_read():
    """Битое ГО (<=0) не кэшируется, иначе сайзинг навсегда падает в фолбэк go_frac
    (×3.4 к плечу). Протухший кэш предпочтительнее фолбэка (аудит 30.07, MED)."""
    from app.st9.service import St9Session
    from app.st4 import tbank_sandbox as sb
    s = St9Session.__new__(St9Session)
    s._go_lot_cache = {}
    calls = {"n": 0}
    def _margin(uid):
        calls["n"] += 1
        return (0.0, 0.0) if calls["n"] == 1 else (12_000.0, 12_000.0)
    # патчим АТРИБУТЫ модуля: `from ..st4 import tbank_sandbox` берёт его из пакета,
    # а не из sys.modules — подмена ключа sys.modules работала бы лишь до первого
    # импорта настоящего модуля (в одиночном прогоне «проходило», в наборе — нет)
    old_ff, old_fm = sb.find_future, sb.futures_margin
    sb.find_future = lambda x: {"uid": "u"}
    sb.futures_margin = _margin
    try:
        assert s._go_per_lot("X", "long") is None      # битое чтение не кэшируется
        assert s._go_per_lot("X", "long") == 12_000.0  # повтор берёт честное значение
        assert s._go_lot_cache[("X", "long")][0] == 12_000.0
    finally:
        sb.find_future, sb.futures_margin = old_ff, old_fm


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
    """Частичное закрытие: движок ведёт остаток (трейл защищает), а закрытая часть
    ФИКСИРУЕТСЯ сделкой. Прежде сделка не записывалась вовсе и её P&L терялся
    навсегда — аудит 10.08 (HIGH-3) признал это дефектом учёта, не задумкой."""
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
    assert len(s.trades) == 1
    assert s.trades[0]["lots"] == 3                    # (81−80)×3×1000 = 3000₽ gross
    assert abs(s.trades[0]["gross_pnl_rub"] - 3000.0) < 1e-6
    assert s.trades[0]["reason"] == "trail_partial"


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
    """Взведённый real после cooldown: ордер идёт в БОЕВОЙ API с sha256-id.
    С 30.07 объём уходит ОДНИМ ордером (было N по 1 лоту) — проверяем и это."""
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
    monkeypatch.setattr(sb, "order_book",
                        lambda uid, depth=10: {"asks": [{"price": 80.0, "qty": 99}],
                                               "bids": [{"price": 79.9, "qty": 99}]})
    monkeypatch.setattr(sb, "future_by_uid",
                        lambda uid: {"minPriceIncrement": {"units": "0", "nano": 10000000}})
    monkeypatch.setattr(live, "post_order",
                        lambda acc, uid, lots, d, oid, **kw:
                        calls.append(oid) or {"lotsExecuted": str(lots)})
    monkeypatch.setattr(sb, "post_order",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("sandbox path!")))
    assert s._order("USDRUBF", 3, "BUY", ref_px=80.0) == 3
    assert len(calls) == 1, f"объём должен уходить одним ордером, ушло {len(calls)}"
    assert len(calls[0]) == 32 and "-" not in calls[0]      # sha256-id, не uuid


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
    # ось БЕЗ риск-сайзинга: instruments[0] (USDRUBF) с 12.08 считает размер от
    # бюджета риска, а эти тесты проверяют режимы НОТИОНАЛА и ПЛЕЧА
    icfg = next(i for i in s.cfg.instruments if not i.risk_per_trade_rub)
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
    # ось БЕЗ риск-сайзинга: instruments[0] (USDRUBF) с 12.08 считает размер от
    # бюджета риска, а эти тесты проверяют режимы НОТИОНАЛА и ПЛЕЧА
    icfg = next(i for i in s.cfg.instruments if not i.risk_per_trade_rub)
    # выкл → по entry_notional_rub оси (величина берётся из конфига, не хардкодом:
    # размер менялся 100к→400к 11.08, тест проверяет МЕХАНИКУ, а не число)
    s.cfg.strategy.go_target_pct = 0.0
    assert s._entry_lots(icfg, 77, 1000) == int(icfg.entry_notional_rub / (77 * 1000))
    # 15% капитала на N осей реестра, go_frac 0.044
    s.cfg.strategy.go_target_pct = 15.0
    lots = s._entry_lots(icfg, 77, 1000)
    notional = lots * 77 * 1000
    expected = 500_000 * 0.15 / sum(1 for i in s.cfg.instruments if i.entries_enabled) / 0.044
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
    # ось БЕЗ риск-сайзинга: instruments[0] (USDRUBF) с 12.08 считает размер от
    # бюджета риска, а эти тесты проверяют режимы НОТИОНАЛА и ПЛЕЧА
    icfg = next(i for i in s.cfg.instruments if not i.risk_per_trade_rub)
    n_axes = sum(1 for i in s.cfg.instruments if i.entries_enabled)            # движков нет → делитель = весь реестр
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
    # ось БЕЗ риск-сайзинга: instruments[0] (USDRUBF) с 12.08 считает размер от
    # бюджета риска, а эти тесты проверяют режимы НОТИОНАЛА и ПЛЕЧА
    icfg = next(i for i in s.cfg.instruments if not i.risk_per_trade_rub)
    notional = s._entry_lots(icfg, 77, 1000) * 77 * 1000
    # должен считать от 500к, не 578к
    expected = 500_000 * 0.15 / sum(1 for i in s.cfg.instruments if i.entries_enabled) / 0.044
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
    # ось БЕЗ риск-сайзинга: instruments[0] (USDRUBF) с 12.08 считает размер от
    # бюджета риска, а эти тесты проверяют режимы НОТИОНАЛА и ПЛЕЧА
    icfg = next(i for i in s.cfg.instruments if not i.risk_per_trade_rub)
    lots = s._entry_lots(icfg, 77, 1000, side="long")
    # целевое ГО на ось = 500к×15%/N осей; лоты = ГО_оси/11500
    expected = int(500_000 * 0.15 / sum(1 for i in s.cfg.instruments if i.entries_enabled) / 11_500.0)
    assert lots == max(1, expected)


def test_sizing_go_fallback_when_api_down():
    """ГО API недоступно (_go_per_lot None) → фолбэк на go_frac-оценку (не падаем)."""
    from app.st9.service import St9Session
    s = St9Session()
    s.capital_sizing_rub = 500_000
    s.cfg.strategy.go_target_pct = 15.0
    s._go_per_lot = lambda sec, side: None      # API недоступно
    # ось БЕЗ риск-сайзинга: instruments[0] (USDRUBF) с 12.08 считает размер от
    # бюджета риска, а эти тесты проверяют режимы НОТИОНАЛА и ПЛЕЧА
    icfg = next(i for i in s.cfg.instruments if not i.risk_per_trade_rub)
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


def test_leverage_divider_independent_of_created_engines():
    """Делитель плеча — ВЕСЬ РЕЕСТР, а не число созданных движков (пересмотр 30.07).
    Раньше делили на len(engines) «чтобы непрогретая ось не забирала долю ГО», но
    движки создаются лениво ВНУТРИ того же tick(), что сайзит входы → первая ось
    делила на 1 и забирала весь бюджет (аудит: ×1.8 суммарно на первом тике после
    рестарта, позиция держится днями). Размер не должен зависеть от момента рестарта."""
    from app.st9.service import St9Session
    from app.st9.engine import St9Engine
    s = St9Session()
    s.capital_sizing_rub = 500_000
    s.cfg.strategy.go_target_pct = 15.0
    s._go_per_lot = lambda sec, side: 11_500.0
    # ось БЕЗ риск-сайзинга: instruments[0] (USDRUBF) с 12.08 считает размер от
    # бюджета риска, а эти тесты проверяют режимы НОТИОНАЛА и ПЛЕЧА
    icfg = next(i for i in s.cfg.instruments if not i.risk_per_trade_rub)
    # реестр ЖИВЫХ осей (выведенные из состава долю бюджета не занимают, 11.08)
    n = sum(1 for i in s.cfg.instruments if i.entries_enabled)
    expected = int(500_000 * 0.15 / n / 11_500.0)
    assert s._entry_lots(icfg, 77, 1000, side="long") == expected   # движков ещё нет
    for sec in ("USDRUBF", "GLDRUBF"):
        s.engines[sec] = St9Engine(sec, 70, 10, 5.0, 14, pv=1000.0)
    assert s._entry_lots(icfg, 77, 1000, side="long") == expected   # и с движками то же


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
    """executedOrderPrice — рублёвая сумма ЗА ОДИН контракт: усредняем ВЗВЕШЕННО по
    лотам. Проверяем на паре «лимит налил часть + добор маркетом»: цена усредняется
    по ОБОИМ ордерам с весом их объёма, а не берётся от последнего."""
    import app.st9.service as svc
    from app.st4 import tbank_sandbox as sb
    s = svc.St9Session()
    s.cfg.mode = "tbank_sandbox"
    s.cfg.account_id = "acc"
    # basicAssetSize=1 → сумма филла равна котировке (GLDRUBF-подобный случай)
    monkeypatch.setattr(sb, "find_future",
                        lambda sec: {"uid": "u1", "basicAssetSize": {"units": "1", "nano": 0}})
    monkeypatch.setattr(sb, "future_by_uid",
                        lambda uid: {"minPriceIncrement": {"units": "0", "nano": 100000000}})
    monkeypatch.setattr(sb, "order_book",
                        lambda uid, depth=10: {"asks": [{"price": 78.0, "qty": 99}],
                                               "bids": [{"price": 77.9, "qty": 99}]})
    # лимит налил 2 лота по 78.2, добор маркетом 2 лота по 78.8 (сумма ЗА КОНТРАКТ)
    resp = iter([
        {"lotsExecuted": "2", "executedOrderPrice": {"units": "78", "nano": 200000000}},
        {"lotsExecuted": "2", "executedOrderPrice": {"units": "78", "nano": 800000000}},
    ])
    monkeypatch.setattr(sb, "post_order", lambda *a, **k: next(resp))
    got = s._order("USDRUBF", 4, "BUY", ref_px=78.0)
    assert got == 4                              # 2 лимитом + 2 добором
    assert abs(s._last_fill_px - 78.5) < 1e-9    # (78.2×2 + 78.8×2)/4 = 78.5


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


def test_backtest_dd_is_mark_to_market():
    """АУДИТ 12.08: stats() считал просадку по ЗАКРЫТЫМ СДЕЛКАМ — плавающий убыток
    открытой позиции в неё не входил, хотя именно его видит оператор на счёте и
    именно он грозит маржин-коллом. Занижение 1.15-1.19× на осях st9.

    Синтетика: цена растёт, потом глубоко проваливается и восстанавливается ДО выхода.
    По закрытым сделкам просадки нет вовсе, mark-to-market — есть."""
    from app.st9.backtest import run, stats, portfolio_dd, _dd_from_curve

    # чистая проверка расчёта: провал 100 -> 60 -> 120
    assert _dd_from_curve([0, 100, 60, 120]) == 40
    assert _dd_from_curve([0, -50, -20]) == 50

    # портфель: просадки осей НЕ складываются, если разнесены во времени
    a = {1: 0.0, 2: -100.0, 3: 0.0}
    b = {1: 0.0, 2: 0.0, 3: -100.0}
    assert portfolio_dd({"a": a, "b": b}) == 100          # не 200

    # ...и складываются, когда совпадают
    c = {1: 0.0, 2: -100.0, 3: 0.0}
    assert portfolio_dd({"a": a, "c": c}) == 200


def test_risk_sizing_scales_inversely_with_volatility():
    """ПЕР-ОСЕВОЙ РИСК-САЙЗИНГ (12.08): лоты = risk / (atr_mult × ATR × pv).

    Смысл — не наращивание размера, а перераспределение: больше лотов в спокойном
    рынке, меньше на всплеске волатильности. Включён ТОЧЕЧНО на USDRUBF (замер: при
    равной просадке +49.5%, выигрыш 5 лет из 5); на IMOEXF режим ВРЕДЕН (−67.5%)."""
    import app.st9.service as svc
    from app.st9.config import St9InstrumentCfg
    s = svc.St9Session()
    s._pv_cache["USDRUBF"] = 1000.0
    icfg = St9InstrumentCfg(secid="USDRUBF", atr_mult=5.0, risk_per_trade_rub=5_000.0)

    # ATR 0.2 → цена стопа на лот = 5.0 × 0.2 × 1000 = 1000₽ → 5 лотов
    assert s._entry_lots(icfg, 80.0, 1000.0, "long", "USDRUBF", atr=0.2) == 5
    # волатильность выросла вдвое → размер ВДВОЕ меньше при том же риске
    assert s._entry_lots(icfg, 80.0, 1000.0, "long", "USDRUBF", atr=0.4) == 2
    # ...и цена входа на размер НЕ влияет (в отличие от режима нотионала)
    assert s._entry_lots(icfg, 160.0, 1000.0, "long", "USDRUBF", atr=0.2) == 5

    # ATR не прогрет → вход не состоится: падать на нотионал нельзя, он для риск-оси
    # не откалиброван и дал бы чужой размер вслепую
    assert s._entry_lots(icfg, 80.0, 1000.0, "long", "USDRUBF", atr=None) == 0

    # ось БЕЗ risk_per_trade_rub считает по-старому, от нотионала
    plain = St9InstrumentCfg(secid="USDRUBF", entry_notional_rub=400_000.0)
    assert s._entry_lots(plain, 80.0, 1000.0, "long", "USDRUBF", atr=0.2) == 5


def test_risk_sizing_enabled_only_on_usdrubf():
    """Режим ПЕР-ОСЕВОЙ: на IMOEXF он ухудшал результат (−67.5% при равной просадке,
    проигрыш 3 года из 4), поэтому включён только там, где замерен выигрыш."""
    from app.st9.config import St9Config
    cfg = St9Config()
    risk = {i.secid: i.risk_per_trade_rub for i in cfg.instruments}
    assert risk["USDRUBF"] == 5_000.0
    assert risk["IMOEXF"] == 0.0
    assert risk["GLDRUBF"] == 0.0


def test_backtest_funding_sign_and_daily_accrual():
    """ФАНДИНГ В МОДЕЛИ (12.08). ST9 торгует ВЕЧНЫМИ фьючерсами, средняя сделка живёт
    4.3 дня, а фандинга в модели издержек не было ВООБЩЕ — при том что st6/st7 его
    считают. Фактические ставки: 20-29% ГОДОВЫХ, больше комиссии и лага вместе.

    Проверяем два инварианта:
    1. ЗНАК — лонг ПЛАТИТ положительный фандинг, шорт ПОЛУЧАЕТ (канон st6/data.py:16).
    2. Начисление РАЗ В КАЛЕНДАРНЫЙ ДЕНЬ, а не на каждый бар: внутри дня баров 10-14,
       поначасовое начисление завысило бы издержки на порядок."""
    from app.st9.backtest import run
    from app.st9.engine import Bar
    import datetime as dt

    # два дня по 3 часовых бара; растущая цена → лонг по пробою
    base = dt.datetime(2026, 3, 2, 10, 0)
    bars = []
    px = 100.0
    for day in range(14):
        for hour in range(3):
            ts = int((base + dt.timedelta(days=day, hours=hour)).timestamp() * 1000)
            px += 0.5
            bars.append(Bar(ts=ts, o=px, h=px + 0.3, l=px - 0.3, c=px))
    swaps = {(base + dt.timedelta(days=d)).date().isoformat(): 1.0 for d in range(14)}

    t_no = run("GLDRUBF", 3, 2, 3.0, bars, notional=10_000.0)
    t_fund = run("GLDRUBF", 3, 2, 3.0, bars, notional=10_000.0, swaps=swaps)
    if not t_no:
        return                                   # сигнала не случилось — нечего сверять
    net_no = sum(x.net_pnl_rub for x in t_no)
    net_f = sum(x.net_pnl_rub for x in t_fund)
    # позиция лонговая (цена растёт) → положительный фандинг УХУДШАЕТ результат
    assert net_f < net_no, (net_no, net_f)

    # шорт при том же положительном фандинге его ПОЛУЧАЕТ: проверяем на падающей цене
    bars_dn = [Bar(ts=b.ts, o=-b.o + 300, h=-b.l + 300, l=-b.h + 300, c=-b.c + 300)
               for b in bars]
    s_no = run("GLDRUBF", 3, 2, 3.0, bars_dn, notional=10_000.0)
    s_f = run("GLDRUBF", 3, 2, 3.0, bars_dn, notional=10_000.0, swaps=swaps)
    if s_no:
        assert sum(x.net_pnl_rub for x in s_f) > sum(x.net_pnl_rub for x in s_no)


def test_backtest_stats_backward_compatible():
    """stats(trades) без curve обязан работать как раньше — на него опираются
    прежние вызовы; dd тогда = просадка по закрытым сделкам, dd_mtm = None."""
    from app.st9.backtest import stats
    from app.st9.engine import St9Trade
    mk = lambda net: St9Trade(secid="X", side="long", entry=1.0, exit=1.0, lots=1,
                              entry_ts=0, exit_ts=0, gross_pnl_rub=net,
                              fees_rub=0.0, net_pnl_rub=net, reason="trail")
    st = stats([mk(100), mk(-40), mk(20)])
    assert st["dd"] == 40 and st["dd_closed"] == 40 and st["dd_mtm"] is None


def test_daily_loss_limit_blocks_entry(monkeypatch):
    """АУДИТ 12.08: daily_loss_limit_rub был МЁРТВОЙ РУЧКОЙ — объявлен в конфиге,
    менялся через API, но в service.py не читался ни разу. Оператор получал
    подтверждение и нулевую защиту, что опаснее отсутствия ручки."""
    import time
    import app.st9.service as svc
    from app.st9.config import St9InstrumentCfg
    s = svc.St9Session()
    s.cfg.mode = "paper"
    s.cfg.strategy.daily_loss_limit_rub = 5_000.0
    now_ms = int(time.time() * 1000)

    # убыток дня в пределах лимита — вход разрешён
    s.trades = [{"net_pnl_rub": -3_000, "exit_ts": now_ms}]
    assert s._daily_loss_hit() is None

    # лимит пробит — вход блокируется
    s.trades.append({"net_pnl_rub": -2_500, "exit_ts": now_ms})
    assert s._daily_loss_hit() == -5_500

    icfg = St9InstrumentCfg(secid="USDRUBF")
    s._pv_cache["USDRUBF"] = 1000.0
    eng = s._engine(icfg)
    monkeypatch.setattr(s, "_order", lambda *a, **k: 5)
    s._apply_signal(eng, {"act": "open", "new_side": "long", "px": 80.0, "atr": 0.5}, icfg)
    assert eng.position is None                      # вход НЕ состоялся
    assert any("дневной лимит" in (e.get("message") or "") for e in s.events)

    # ...но ВЫХОД от лимита не зависит: иначе позиция залипла бы навсегда
    eng.open("long", 80.0, 5, 1, atr=0.5)
    s._apply_signal(eng, {"act": "close", "px": 82.0, "reason": "trail"}, icfg)
    assert eng.position is None


def test_daily_loss_limit_counts_only_today_msk():
    """Лимит считает P&L ТЕКУЩЕГО дня по МСК, вчерашние убытки не блокируют сегодня.
    ⚠️ exit_ts — настоящий epoch (time.time()), а не сдвинутая шкала баров
    _now_ms_frame: смешивать их нельзя, обе стороны считаются от UTC+3."""
    import time
    import app.st9.service as svc
    s = svc.St9Session()
    s.cfg.strategy.daily_loss_limit_rub = 5_000.0
    now_ms = int(time.time() * 1000)
    s.trades = [{"net_pnl_rub": -50_000, "exit_ts": now_ms - 3 * 86400 * 1000}]
    assert s._daily_loss_hit() is None               # позавчерашний убыток не в счёт
    s.trades.append({"net_pnl_rub": -6_000, "exit_ts": now_ms})
    assert s._daily_loss_hit() == -6_000             # только сегодняшний

    s.cfg.strategy.daily_loss_limit_rub = 0.0        # 0 = выключен
    assert s._daily_loss_hit() is None


def test_disabled_axis_blocks_entry_but_keeps_exit(monkeypatch):
    """ВЫВОД ОСИ ИЗ СОСТАВА (GLDRUBF, 11.08): новых входов нет, ВЫХОД работает.

    Флаг, а не удаление из реестра: удалять ось с ОТКРЫТОЙ позицией нельзя — лоты
    остались бы на счёте без трейла и без выхода (голая позиция)."""
    import app.st9.service as svc
    from app.st9.config import St9InstrumentCfg
    s = svc.St9Session()
    s.cfg.mode = "paper"
    icfg = St9InstrumentCfg(secid="USDRUBF", entries_enabled=False)
    s._pv_cache["USDRUBF"] = 1000.0
    eng = s._engine(icfg)
    monkeypatch.setattr(s, "_order", lambda *a, **k: 5)

    # вход НЕ должен состояться
    s._apply_signal(eng, {"act": "open", "new_side": "long", "px": 80.0, "atr": 0.5}, icfg)
    assert eng.position is None

    # ...но выход по уже открытой позиции обязан работать
    eng.open("long", 80.0, 5, 1, atr=0.5)
    s._apply_signal(eng, {"act": "close", "px": 82.0, "reason": "trail"}, icfg)
    assert eng.position is None and len(s.trades) == 1

    # и reverse не должен открывать встречную — только закрыть
    eng.open("long", 80.0, 5, 1, atr=0.5)
    s._apply_signal(eng, {"act": "reverse", "new_side": "short", "px": 78.0,
                          "reason": "reverse", "atr": 0.5}, icfg)
    assert eng.position is None


def test_disabled_axis_excluded_from_margin_divider():
    """Выведенная ось не занимает долю бюджета ГО при плече — иначе оставшиеся оси
    сайзятся меньше, чем должны (цена простоя GAZR, 30.07: ≈3.5 п.п. годовых)."""
    import app.st9.service as svc
    s = svc.St9Session()
    s.capital_sizing_rub = 600_000
    s.cfg.strategy.go_target_pct = 15.0
    monkeypatch_go = 10_000.0
    # IMOEXF, а не USDRUBF: последняя с 12.08 на риск-сайзинге и режим плеча минует
    s._go_lot_cache[("IMOEXF", "long")] = (monkeypatch_go, __import__("time").time())
    icfg = next(i for i in s.cfg.instruments if i.secid == "IMOEXF")
    lots_3axes = s._entry_lots(icfg, 80.0, 1000.0, "long", "IMOEXF")

    # GLDRUBF выведена → бюджет делится на 2 живые оси, не на 3
    live = sum(1 for i in s.cfg.instruments if i.entries_enabled)
    assert live == 2
    assert lots_3axes == int(600_000 * 0.15 / live / monkeypatch_go)


def test_execution_gap_detects_hidden_costs():
    """АУДИТ 10.08 MED-1: сверка журнала со СЧЁТОМ. До 11.08 exec_anchor писался и
    персистился, но НЕ ЧИТАЛСЯ — канон «истина = счёт» у ST9 не был реализован."""
    import app.st9.service as svc
    s = svc.St9Session()
    s.cfg.mode = "tbank_sandbox"
    s.cfg.account_id = "acc"
    s.exec_anchor = {"capital_sizing": 500_000.0, "net": 0.0, "account_id": "acc"}

    # журнал говорит +10 000, счёт вырос ровно на столько → расхождения нет
    s.trades = [{"net_pnl_rub": 10_000}]
    s.capital_sizing_rub = 510_000.0
    assert s._execution_gap() == 0

    # журнал говорит +10 000, а счёт принёс только 7 000 → скрытые издержки 3 000
    s.capital_sizing_rub = 507_000.0
    assert s._execution_gap() == -3000

    # чужой счёт / нет якоря / paper — сверять нечего
    s.cfg.account_id = "other"
    assert s._execution_gap() is None
    s.cfg.account_id = "acc"
    s.exec_anchor = None
    assert s._execution_gap() is None


def test_execution_gap_ignores_legacy_anchor():
    """Якорь старого формата (на totalAmountPortfolio, без capital_sizing) НЕ годится:
    сверка модели с mark-to-market серией дала бы «разрыв» размером с переоценку
    фьючерса, а не с издержками. Такой якорь игнорируется до перестановки."""
    import app.st9.service as svc
    s = svc.St9Session()
    s.cfg.mode = "tbank_sandbox"
    s.cfg.account_id = "acc"
    s.capital_sizing_rub = 500_000.0
    s.exec_anchor = {"capital": 900_000.0, "net": 0.0, "account_id": "acc"}
    assert s._execution_gap() is None


def test_execution_gap_only_when_flat():
    """Сверка ТОЛЬКО во флэте: `free + ГО` не содержит вариационной маржи (ГО — залог,
    от хода цены не меняется), а модель с unrealized — содержит. При открытой позиции
    разрыв равнялся бы вармарже, а не издержкам.

    Замер на проде 11.08 поймал это в бою: разрыв −23 901₽ при unrealized +24 217₽ —
    метрика мерила плавающую прибыль, а не стоимость исполнения."""
    import app.st9.service as svc
    from app.st9.config import St9InstrumentCfg
    s = svc.St9Session()
    s.cfg.mode = "tbank_sandbox"
    s.cfg.account_id = "acc"
    s.capital_sizing_rub = 500_000.0
    s.exec_anchor = {"capital_sizing": 500_000.0, "net": 0.0, "account_id": "acc"}
    assert s._execution_gap() == 0             # флэт → сверка работает

    s._pv_cache["USDRUBF"] = 1000.0
    eng = s._engine(St9InstrumentCfg(secid="USDRUBF"))
    eng.open("long", 80.0, 2, 1, atr=0.5)
    assert s._execution_gap() is None          # позиция открыта → не сверяем


def test_execution_anchor_not_set_while_position_open(monkeypatch, tmp_path):
    """Якорь ставится только во ФЛЭТЕ: при открытой позиции в базу попала бы вармаржа,
    которой нет в модели, и разрыв врал бы на её величину навсегда. Плюс якорь обязан
    ПЕРСИСТИТЬСЯ — иначе рестарт обнуляет накопленную сверку."""
    import app.st9.service as svc
    from app.st4 import tbank_sandbox as sb
    from app.st9.config import St9InstrumentCfg
    s = svc.St9Session()
    s.cfg.mode = "tbank_sandbox"
    s.cfg.account_id = "acc"
    s._session_file = tmp_path / "s9.json"
    monkeypatch.setattr(sb, "portfolio",
                        lambda acc: {"totalAmountPortfolio": {"units": "500000", "nano": 0}})
    monkeypatch.setattr(sb, "free_money_rub", lambda acc: 480_000.0)
    monkeypatch.setattr(sb, "find_future", lambda sec: {"uid": "u1"})
    monkeypatch.setattr(sb, "futures_margin", lambda uid: (10_000.0, 10_000.0))

    s._pv_cache["USDRUBF"] = 1000.0
    eng = s._engine(St9InstrumentCfg(secid="USDRUBF"))
    eng.open("long", 80.0, 2, 1, atr=0.5)
    s.refresh_capital()
    assert s.exec_anchor is None               # позиция открыта → якорь НЕ ставим

    eng.position = None
    s.refresh_capital()
    assert s.exec_anchor is not None and s.exec_anchor.get("capital_sizing")
    assert "exec_anchor" in s._session_file.read_text()   # персистнут


def test_entry_notional_is_400k_and_margin_stays_safe():
    """РАЗМЕР 400к/ось (решение оператора 11.08) + проверка, что ГО в стрессе не
    подходит к капиталу. При 100к утилизация была 4.1% ГО → 3.3%/год на счёт 764к.

    ГО берётся ФАКТИЧЕСКОЕ (замер GetFuturesMargin 11.08), не go_frac=0.044 — та
    константа занижает ГО втрое и служит лишь аварийным фолбэком."""
    from app.st9.config import St9Config
    cfg = St9Config()
    assert [i.entry_notional_rub for i in cfg.instruments] == [400_000.0] * 3

    capital = 764_003
    # (ГО на лот, цена, pv) — факт с брокера
    axes = {"USDRUBF": (12336, 82.26, 1000.0),
            "GLDRUBF": (1260, 11536.30, 1.0),
            "IMOEXF": (2287, 2313.50, 10.0)}
    go_total = sum(int(400_000 / (px * pv)) * go for go, px, pv in axes.values())
    assert go_total / capital < 0.20              # ~17% — обычный режим
    # биржа поднимает ГО вдвое в волатильность, ровно когда мы в просадке
    assert go_total * 2 / capital < 0.40          # ~34% — запас до маржин-колла есть


def test_save_session_is_atomic_and_reports_failure(tmp_path, monkeypatch):
    """АУДИТ 10.08: персист атомарен (tmp+os.replace) и НЕ глотает ошибку.

    write_text рвал файл на середине при OOM → load_session падал на битом JSON и
    стартовал БЕЗ позиций, пока лоты живут на счёте. А молчаливый except делал
    недостижимой ветку «🚨 состояние НЕ сохранено» в обработчиках _apply_signal."""
    import app.st9.service as svc
    s = svc.St9Session()
    s._session_file = tmp_path / "session_state_9.json"
    s.save_session()
    assert s._session_file.exists()
    assert not (tmp_path / "session_state_9.json.tmp").exists()   # tmp убран за собой

    # провал записи обязан быть виден в событиях, а не проглочен
    def _boom(*a, **k):
        raise OSError("No space left on device")

    monkeypatch.setattr(svc.Path, "write_text", _boom)
    s.save_session()                                   # не должно бросить наружу
    assert any("НЕ сохранено" in (e.get("message") or "") for e in s.events)


def test_close_partial_journals_pnl_and_splits_entry_fee():
    """АУДИТ 10.08 HIGH-3: закрытая часть попадает в журнал, входная комиссия делится.

    Было: при недоливе движок делал lots -= got и молча уходил — P&L закрытых лотов
    терялся НАВСЕГДА, а комиссия входа за полный объём оставалась на остатке."""
    from app.st9.engine import St9Engine
    eng = St9Engine(secid="USDRUBF", don_enter=20, don_exit=10, atr_mult=3.0,
                    atr_period=14, pv=1000.0, fee_pct_notional=0.05)
    eng.open("long", 80.0, 10, 1000, atr=1.0)
    entry_fee = eng.position.fees_rub                 # 80×1000×10×0.0005 = 400
    assert abs(entry_fee - 400.0) < 1e-6

    tr = eng.close_partial(82.0, 4, 2000, "trail_partial")
    # gross закрытой части: (82-80)×1×4×1000 = 8000 — раньше терялся целиком
    assert abs(tr.gross_pnl_rub - 8000.0) < 1e-6
    assert tr.lots == 4
    # комиссия части: 4/10 входной (160) + выходная 82×1000×4×0.0005 (164)
    assert abs(tr.fees_rub - (160.0 + 164.0)) < 1e-6

    # остаток несёт ТОЛЬКО свою долю входной комиссии, не всю
    assert eng.position.lots == 6
    assert abs(eng.position.fees_rub - 240.0) < 1e-6

    # закрытие остатка: комиссия входа не задваивается
    tr2 = eng.close(83.0, 3000, "trail")
    assert tr2.lots == 6
    assert abs(tr2.fees_rub - (240.0 + 83.0 * 1000 * 6 * 0.0005)) < 1e-6
    # суммарная входная комиссия по обеим сделкам = ровно исходные 400₽
    assert abs((160.0 + 240.0) - entry_fee) < 1e-6


def test_partial_limit_cancels_rest_before_market_topup(monkeypatch):
    """АУДИТ 10.08 HIGH-1: недолитый лимитник ОБЯЗАН быть снят из стакана до добора.

    Иначе он наливается позже (цена вернулась к потолку) и на счёте оказывается БОЛЬШЕ
    лотов, чем ведёт движок: лишние — голая позиция без трейла, выход её не закроет.
    Канон st5 (executor.py:153). Проверяем И факт отмены, И ПОРЯДОК: отмена строго
    перед добором, иначе между ними остаётся то же окно."""
    import app.st9.service as svc
    from app.st4 import tbank_sandbox as sb
    s = svc.St9Session()
    s.cfg.mode = "tbank_sandbox"
    s.cfg.account_id = "acc"
    monkeypatch.setattr(sb, "find_future",
                        lambda sec: {"uid": "u1", "basicAssetSize": {"units": "1", "nano": 0}})
    monkeypatch.setattr(sb, "future_by_uid",
                        lambda uid: {"minPriceIncrement": {"units": "0", "nano": 100000000}})
    monkeypatch.setattr(sb, "order_book",
                        lambda uid, depth=10: {"asks": [{"price": 78.0, "qty": 99}],
                                               "bids": [{"price": 77.9, "qty": 99}]})
    calls = []
    # лимит налил 4 из 10 и вернул orderId; добор маркетом на остаток
    resp = iter([
        {"lotsExecuted": "4", "orderId": "oid-limit-1",
         "executedOrderPrice": {"units": "78", "nano": 0}},
        {"lotsExecuted": "6", "executedOrderPrice": {"units": "79", "nano": 0}},
    ])

    def _post(*a, **k):
        calls.append("order")
        return next(resp)

    monkeypatch.setattr(sb, "post_order", _post)
    monkeypatch.setattr(sb, "cancel_order",
                        lambda acc, oid: calls.append(f"cancel:{oid}"))
    got = s._order("USDRUBF", 10, "BUY", ref_px=78.0)
    assert got == 10                                   # 4 лимитом + 6 добором
    assert calls == ["order", "cancel:oid-limit-1", "order"], calls


def test_cancel_rest_failure_is_logged_not_fatal(monkeypatch):
    """Отмена не удалась — ордер истечёт сам, но операцию нельзя проглотить молча:
    незамеченный висящий лимитник и есть источник рассинхрона."""
    import app.st9.service as svc
    from app.st4 import tbank_sandbox as sb
    s = svc.St9Session()
    s.cfg.account_id = "acc"

    def _boom(acc, oid):
        raise RuntimeError("сеть легла")

    monkeypatch.setattr(sb, "cancel_order", _boom)
    s._cancel_rest({"orderId": "oid-x"}, real=False)   # не должно бросить
    assert any("не снят остаток" in (e.get("message") or "") for e in s.events)


def test_fill_price_multilot_with_basic_asset_size(monkeypatch):
    """РЕГРЕССИЯ 10.08: много лотов И basicAssetSize>1 одновременно. Прежние два теста
    брали либо 1 лот на слайс, либо bas=1 — комбинация, живущая в проде, не покрывалась,
    и лишнее деление на лоты прошло незамеченным. Симптом: USDRUBF 2 лота дал филл
    40.38 при цене бара 80.61 (ровно px/лоты) → slip_rub=None гейтом правдоподобия,
    замер издержек молча умер после перехода на ордер одним объёмом (fc4d827)."""
    import app.st9.service as svc
    from app.st4 import tbank_sandbox as sb
    s = svc.St9Session()
    s.cfg.mode = "tbank_sandbox"
    s.cfg.account_id = "acc"
    monkeypatch.setattr(sb, "find_future",
                        lambda sec: {"uid": "u1", "basicAssetSize": {"units": "1000", "nano": 0}})
    monkeypatch.setattr(sb, "future_by_uid",
                        lambda uid: {"minPriceIncrement": {"units": "0", "nano": 10000000}})
    monkeypatch.setattr(sb, "order_book",
                        lambda uid, depth=10: {"asks": [{"price": 80.7, "qty": 999}],
                                               "bids": [{"price": 80.6, "qty": 999}]})
    # 2 лота одним ордером; сумма ЗА КОНТРАКТ = 80.61 × bas(1000) = 80610
    monkeypatch.setattr(sb, "post_order", lambda *a, **k: {
        "lotsExecuted": "2", "executedOrderPrice": {"units": "80610", "nano": 0}})
    assert s._order("USDRUBF", 2, "BUY", ref_px=80.61) == 2
    assert abs(s._last_fill_px - 80.61) < 1e-6         # котировка, НЕ 40.305


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


def test_trade_carries_entry_fee_share():
    """Сделка хранит долю комиссии, уплаченную в день ВХОДА (сверка журнал↔счёт).

    fees_rub = вход + выход, но счёт списывает эти части в РАЗНЫЕ дни. Без
    entry_fees_rub сверка относила всю сумму ко дню закрытия → ложная ⚠️."""
    e = _eng(fee_per_lot=10.0)
    _feed_flat(e, 8)
    e.open("long", 100.0, 3, 9, 1.0)
    entry_fee = e.position.fees_rub
    tr = e.close(110.0, 20, "trail")
    assert entry_fee > 0
    assert tr.entry_fees_rub == round(entry_fee, 2)
    # выходная часть = остаток; вместе дают полную комиссию сделки
    assert round(tr.fees_rub - tr.entry_fees_rub, 2) > 0
    assert tr.net_pnl_rub == round(tr.gross_pnl_rub - tr.fees_rub, 2)


def test_partial_close_splits_entry_fee_proportionally():
    """При частичном закрытии в сделку попадает ТОЛЬКО своя доля входной комиссии."""
    e = _eng(fee_per_lot=10.0)
    _feed_flat(e, 8)
    e.open("long", 100.0, 10, 9, 1.0)
    full_entry = e.position.fees_rub
    tr = e.close_partial(110.0, 4, 20, "partial")
    assert tr.entry_fees_rub == round(full_entry * 4 / 10, 2)
    # остаток сохранил свою долю — сумма частей не превышает исходную
    assert round(tr.entry_fees_rub + e.position.fees_rub, 2) == round(full_entry, 2)


def test_ledger_recon_splits_fee_across_days(monkeypatch):
    """Комиссия разносится по дням ФАКТИЧЕСКОГО списания счётом, а не валится на выход.

    Инцидент 13.08: сделка вошла 12-го, вышла 13-го → вся fees_rub (вход+выход) легла
    на 13-е, тогда как счёт списал вход 12-го. Итог — ложная ⚠️ Δ-211 при сошедшемся
    по существу учёте."""
    import datetime as _dtm
    import app.api as api
    from app.st4 import tbank_sandbox as _sb
    MSK = _dtm.timezone(_dtm.timedelta(hours=3))

    def _ms(day, hour=12):
        return int(_dtm.datetime.strptime(day, "%Y-%m-%d")
                   .replace(hour=hour, tzinfo=MSK).timestamp() * 1000)

    # вход 12.08, выход 13.08; комиссия 300 = вход 100 + выход 200
    monkeypatch.setattr(api.ST9, "trades",
                        [{"exit_ts": _ms("2026-08-13"), "entry_ts": _ms("2026-08-12"),
                          "fees_rub": 300.0, "entry_fees_rub": 100.0}], raising=False)
    monkeypatch.setattr(api.ST9, "engines", {}, raising=False)
    monkeypatch.setattr(api.ST8, "engines", {}, raising=False)
    monkeypatch.setattr(api.ST8, "trades", [], raising=False)
    api.ST9.cfg.mode = "tbank_sandbox"; api.ST9.cfg.account_id = "acc-test"
    api.ST8.cfg.mode = "paper"; api.ST8.cfg.account_id = ""
    monkeypatch.setattr(_sb, "_call", lambda *a, **k: {"operations": []})
    monkeypatch.setattr(_sb, "_account_token", lambda acc: "t")

    def _fee_of(day):
        return {r[0].split(" ")[0]: r[2] for r in api._daily_ledger_recon(day, MSK)}["ST9"]

    assert _fee_of("2026-08-12") == 100.0   # входная часть — в день ВХОДА
    assert _fee_of("2026-08-13") == 200.0   # выходная часть — в день ВЫХОДА
    assert _fee_of("2026-08-14") == 0.0     # посторонний день чист


def test_ledger_recon_old_trades_without_split(monkeypatch):
    """Сделки до 14.08 не имеют entry_fees_rub — вся комиссия относится ко дню закрытия.

    Иначе исторические сделки давали бы нулевую журнальную сторону и ложную ⚠️."""
    import datetime as _dtm
    import app.api as api
    from app.st4 import tbank_sandbox as _sb
    MSK = _dtm.timezone(_dtm.timedelta(hours=3))
    ts = int(_dtm.datetime.strptime("2026-08-13", "%Y-%m-%d")
             .replace(hour=12, tzinfo=MSK).timestamp() * 1000)
    monkeypatch.setattr(api.ST9, "trades",
                        [{"exit_ts": ts, "entry_ts": ts - 86400_000,
                          "fees_rub": 300.0}], raising=False)   # без entry_fees_rub
    monkeypatch.setattr(api.ST9, "engines", {}, raising=False)
    monkeypatch.setattr(api.ST8, "engines", {}, raising=False)
    monkeypatch.setattr(api.ST8, "trades", [], raising=False)
    api.ST9.cfg.mode = "tbank_sandbox"; api.ST9.cfg.account_id = "acc-test"
    api.ST8.cfg.mode = "paper"; api.ST8.cfg.account_id = ""
    monkeypatch.setattr(_sb, "_call", lambda *a, **k: {"operations": []})
    monkeypatch.setattr(_sb, "_account_token", lambda acc: "t")
    rows = {r[0].split(" ")[0]: r[2] for r in api._daily_ledger_recon("2026-08-13", MSK)}
    assert rows["ST9"] == 300.0


def test_entry_fee_survives_restart_into_trade(tmp_path, monkeypatch):
    """Позиция, открытая ДО появления entry_fees_rub, после рестарта всё равно даёт
    правильную разбивку: входная комиссия живёт в position.fees_rub и персистится.

    Проверка перед деплоем 14.08: три открытые позиции прода закроются корректно,
    миграция журнала не нужна."""
    import json
    import app.st9.service as svc
    f = tmp_path / "s9.json"
    # session-файл в формате прода: позиция с уплаченной комиссией входа
    f.write_text(json.dumps({
        "positions": {"USDRUBF": {"side": "long", "entry": 83.13, "lots": 6,
                                  "entry_ts": 1786533308397, "trail": 83.36,
                                  "fees_rub": 261.39}},
        "trades": [], "state": {}, "config": {},
    }), encoding="utf-8")
    s = svc.St9Session()
    s._session_file = f
    s.load_session()
    # позиция уже в движке (или ждёт в pending, если pv был недоступен)
    eng = s.engines.get("USDRUBF")
    pos = eng.position if eng and eng.position else svc.St9Position(
        **s._pending_positions["USDRUBF"])
    assert pos.fees_rub == 261.39               # комиссия входа пережила рестарт

    e = _eng(secid="USDRUBF", pv=1.0, fee_per_lot=0.0)
    e.position = pos
    tr = e.close(84.0, 1786600000000, "trail")
    assert tr.entry_fees_rub == 261.39          # уплачено в день ВХОДА
    assert tr.fees_rub >= tr.entry_fees_rub     # плюс выходная часть


def test_update_strategy_applies_fee_to_live_engines(monkeypatch):
    """Смена модели издержек доходит до УЖЕ СОЗДАННЫХ движков.

    Параметры комиссии читаются в St9Engine при создании, а движки кэшируются в
    self.engines — без проброса правка молча не действовала бы до рестарта, выглядя
    применённой. Ровно так на проде до 14.08 жил fee_per_lot=2.0 (фиктивная модель,
    заменённая на 0.05% нотионала ещё 30.07)."""
    import app.st9.service as svc
    s = svc.St9Session()
    s.cfg.strategy.fee_per_lot = 2.0
    s.cfg.strategy.fee_pct_notional = 0.05
    monkeypatch.setattr(s, "save_session", lambda: None)
    e = _eng(secid="IMOEXF", pv=10.0, fee_per_lot=2.0)
    e.fee_pct_notional = 0.05
    s.engines["IMOEXF"] = e
    # 2₽/лот завышают комиссию: 20 лотов @2267 → 226.70 (0.05%) + 40 лишних
    assert round(e._fee(2267.0, 20), 2) == 266.70

    out = s.update_strategy({"fee_per_lot": 0.0})
    assert out["fee_per_lot"] == 0.0
    assert e.fee_per_lot == 0.0                      # проброшено в ЖИВОЙ движок
    assert round(e._fee(2267.0, 20), 2) == 226.70    # чистые 0.05% нотионала


def test_update_strategy_rejects_bad_fee():
    """Комиссия вне диапазона отвергается (защита от опечатки в боевом параметре)."""
    import pytest
    import app.st9.service as svc
    s = svc.St9Session()
    with pytest.raises(ValueError):
        s.update_strategy({"fee_pct_notional": 5.0})   # 5% — заведомо опечатка
    with pytest.raises(ValueError):
        s.update_strategy({"fee_per_lot": -1.0})
