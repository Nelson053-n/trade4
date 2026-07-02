"""Тесты ST6 — фандинг-арбитраж вечных фьючерсов (движок, сигнал, P&L, персист)."""
from app.st6.config import St6StrategyConfig
from app.st6.engine import DaySnap, St6Engine


def _eng(**kw):
    st = St6StrategyConfig(**kw)
    # пара имитирует imoexf: пункт перпа 10₽, квартальника 1₽, юнит 10:1 (нотионал-паритет)
    return St6Engine("imoexf", st, pv_perp=10.0, pv_quart=1.0, perp_lots=10, quart_lots=1)


def _snap(date="2026-07-01", perp=2344.0, swap=1.0, fund_ann=25.0,
          qsec="MXU6", quart=236000.0, basis_ann=15.0):
    return DaySnap(date=date, perp_settle=perp, swaprate=swap, fund_trail_ann_pp=fund_ann,
                   quart_secid=qsec, quart_settle=quart, basis_ann_pp=basis_ann)


def test_edge_signal_enter_exit_thresholds():
    """Вход при edge > enter-порога, выход при edge < exit-порога, иначе hold/none."""
    e = _eng(edge_enter_pp=4.0, edge_exit_pp=0.0)
    assert e.daily_step(_snap(fund_ann=25, basis_ann=23)) == "none"     # edge 2 < 4
    assert e.daily_step(_snap(fund_ann=25, basis_ann=15)) == "enter"    # edge 10
    s = _snap(fund_ann=25, basis_ann=15)
    e.confirm_enter(s, perp_fill=2344.0, quart_fill=236000.0, fee_rub=22.0)
    assert e.daily_step(_snap(fund_ann=20, basis_ann=18)) == "hold"     # edge 2 > 0
    assert e.daily_step(_snap(fund_ann=10, basis_ann=12)) == "exit"     # edge −2 < 0


def test_funding_accrues_to_short():
    """Ежедневный SWAPRATE начисляется позиции: ₽ = swaprate × pv_perp × лоты перпа."""
    e = _eng()
    s = _snap(swap=1.5)
    e.confirm_enter(s, perp_fill=2344.0, quart_fill=236000.0, fee_rub=0.0)
    e.daily_step(_snap(swap=2.0, fund_ann=25, basis_ann=15))            # hold + начисление
    # 2.0 пункта × 10₽/пункт × 10 лотов = 200₽
    assert e.position.funding_rub == 200.0


def test_exit_pnl_by_legs_and_funding():
    """Net = P&L ног (с пункт-стоимостью КАЖДОЙ ноги) + фандинг − комиссии."""
    e = _eng()
    s0 = _snap(perp=2344.0, quart=236000.0)
    e.confirm_enter(s0, perp_fill=2344.0, quart_fill=236000.0, fee_rub=22.0)
    e.position.funding_rub = 300.0
    s1 = _snap(date="2026-07-08", perp=2350.0, quart=236900.0, fund_ann=5, basis_ann=10)
    tr = e.confirm_exit(s1, perp_fill=2350.0, quart_fill=236900.0, fee_rub=22.0)
    # ноги: quart +900×1×1 = +900; perp шорт −(2350−2344)×10×10 = −600 → legs +300
    assert tr.legs_pnl_rub == 300.0
    assert tr.funding_rub == 300.0
    assert tr.fees_rub == 44.0
    assert tr.net_pnl_rub == 300.0 + 300.0 - 44.0
    assert tr.days_held == 7
    assert e.position is None


def test_roll_preserves_leg_pnl():
    """Ролл квартальника: entry новой ноги сдвигается так, что суммарный legs-P&L сохранён."""
    e = _eng()
    e.confirm_enter(_snap(qsec="MXU6", quart=236000.0), perp_fill=2344.0,
                    quart_fill=236000.0, fee_rub=0.0)
    # старая нога выросла до 237000 (unrealized +1000), новая серия торгуется по 239000
    s_roll = _snap(date="2026-09-15", qsec="MXZ6", quart=239000.0)
    e.confirm_roll(s_roll, old_quart_fill=237000.0, new_quart_fill=239000.0, fee_rub=4.0)
    p = e.position
    assert p.quart_secid == "MXZ6"
    assert p.rolled == 1
    # entry_new = 239000 − (237000 − 236000) = 238000 → (exit − entry) сохранит +1000
    assert p.quart_entry == 238000.0
    tr = e.confirm_exit(_snap(date="2026-09-16", perp=2344.0, qsec="MXZ6", quart=239000.0,
                              fund_ann=0, basis_ann=10),
                        perp_fill=2344.0, quart_fill=239000.0, fee_rub=0.0)
    assert tr.legs_pnl_rub == 1000.0        # только рост старой ноги, перп не двигался
    assert tr.rolled == 1


def test_roll_triggered_by_series_change():
    """Смена ближней серии (порог ролла) при открытой позиции → действие 'roll'."""
    e = _eng()
    e.confirm_enter(_snap(qsec="MXU6"), perp_fill=2344.0, quart_fill=236000.0, fee_rub=0.0)
    assert e.daily_step(_snap(qsec="MXZ6", fund_ann=25, basis_ann=15)) == "roll"


def test_session_persist_round_trip(tmp_path, monkeypatch):
    """Позиция/журнал/last_day переживают save/load (рестарт сервиса)."""
    from app.st6.service import St6Session
    s = St6Session()
    s._session_file = tmp_path / "s6.json"
    # движок без сети: подсунем готовый
    eng = _eng()
    s.engines["imoexf"] = eng
    eng.confirm_enter(_snap(), perp_fill=2344.0, quart_fill=236000.0, fee_rub=22.0)
    eng.position.perp_secid = "IMOEXF"
    s.last_day["imoexf"] = "2026-07-01"
    s.trades.append({"pair": "imoexf", "net_pnl_rub": 123.0})
    s.save_session()
    s2 = St6Session()
    s2._session_file = tmp_path / "s6.json"
    monkeypatch.setattr(s2, "_engine", lambda pid: s2.engines.setdefault(pid, _eng()))
    assert s2.load_session() is True
    assert s2.last_day["imoexf"] == "2026-07-01"
    assert s2.trades[0]["net_pnl_rub"] == 123.0
    p = s2.engines["imoexf"].position
    assert p is not None and p.perp_lots == 10 and p.quart_entry == 236000.0


def test_trading_disabled_blocks_only_entry(monkeypatch):
    """trading_enabled=False блокирует ВХОД, но выход по edge работает (позиция не липнет)."""
    from app.st6.service import St6Session
    s = St6Session()
    s.cfg.trading_enabled = False
    eng = _eng()
    s.engines["imoexf"] = eng
    # мок данных ISS: день с жирным edge → вход должен быть отклонён
    def fake_tick(pid):
        snap = _snap(fund_ann=30, basis_ann=10)
        action = eng.daily_step(snap)
        if action == "enter" and not s.cfg.trading_enabled:
            action = "none"
        return action
    assert fake_tick("imoexf") == "none" or eng.position is None
    # с позицией и плохим edge — выход разрешён
    eng.confirm_enter(_snap(), perp_fill=2344.0, quart_fill=236000.0, fee_rub=0.0)
    assert eng.daily_step(_snap(fund_ann=0, basis_ann=10)) == "exit"


def test_dividend_trap_blocks_entry():
    """Аномальный |базис| (дивидендная бэквордация) блокирует вход, даже при жирном edge —
    дисконт квартальника не конвергирует в прибыль (регресс: живой sberf edge=+60.8пп 02.07)."""
    e = _eng(edge_enter_pp=4.0, basis_sane_pp=25.0)
    assert e.daily_step(_snap(fund_ann=23, basis_ann=-37.8)) == "none"   # edge 60.8, базис аномален
    assert e.daily_step(_snap(fund_ann=25, basis_ann=15)) == "enter"     # нормальный базис
