"""Тесты ST7 — «фандинг-давление» (сигнал, полухедж-P&L, ролл, персист)."""
from app.st6.engine import DaySnap
from app.st7.config import St7StrategyConfig
from app.st7.engine import St7Engine


def _eng(**kw):
    st = St7StrategyConfig(**kw)
    # imoexf: пункт перпа 10₽, кварт 1₽; полухедж-юнит 20 перпов : 1 MX
    return St7Engine("imoexf", st, pv_perp=10.0, pv_quart=1.0, perp_lots=20, quart_lots=1)


def _snap(date="2026-07-02", perp=2344.0, swap=1.0, fund=40.0, qsec="MXU6",
          quart=236000.0, basis=15.0):
    return DaySnap(date=date, perp_settle=perp, swaprate=swap, fund_trail_ann_pp=fund,
                   quart_secid=qsec, quart_settle=quart, basis_ann_pp=basis)


def test_signal_thresholds_and_trap():
    """Вход при фандинге>enter, выход при <exit; дивидендная аномалия базиса → trap."""
    e = _eng(fund_enter_pp=35.0, fund_exit_pp=25.0, basis_sane_pp=25.0)
    assert e.daily_step(_snap(fund=30)) == "none"
    assert e.daily_step(_snap(fund=40, basis=-38)) == "trap"
    assert e.daily_step(_snap(fund=40)) == "enter"
    e.confirm_enter(_snap(fund=40), perp_fill=2344.0, quart_fill=236000.0, fee_rub=42.0)
    assert e.daily_step(_snap(fund=30)) == "hold"     # 25<30<35 — держим
    assert e.daily_step(_snap(fund=20)) == "exit"


def test_half_hedge_pnl_asymmetry():
    """Полухедж: падение рынка даёт ~половину нотионала прибыли (шорт 2×, хедж 1×)."""
    e = _eng()
    e.confirm_enter(_snap(perp=2344.0, quart=234400.0), perp_fill=2344.0,
                    quart_fill=234400.0, fee_rub=0.0)
    # рынок −1%: перп −23.44 (шорт 20×10₽ → +4688), кварт −2344 (лонг 1×1₽ → −2344)
    tr = e.confirm_exit(_snap(date="2026-07-09", perp=2320.56, quart=232056.0, fund=10),
                        perp_fill=2320.56, quart_fill=232056.0, fee_rub=0.0)
    assert abs(tr.legs_pnl_rub - (4688.0 - 2344.0)) < 1.0   # ≈ половина полного шорта
    assert tr.days_held == 7


def test_funding_accrues_on_full_short():
    """Фандинг начисляется с ПОЛНОГО шорта (2×паритет), не с нетто-экспозиции."""
    e = _eng()
    e.confirm_enter(_snap(), perp_fill=2344.0, quart_fill=236000.0, fee_rub=0.0)
    e.daily_step(_snap(swap=2.0, fund=40))
    assert e.position.funding_rub == 2.0 * 10.0 * 20      # 400₽ — все 20 лотов


def test_roll_preserves_pnl():
    e = _eng()
    e.confirm_enter(_snap(qsec="MXU6", quart=236000.0), perp_fill=2344.0,
                    quart_fill=236000.0, fee_rub=0.0)
    e.confirm_roll(_snap(date="2026-09-15", qsec="MXZ6", quart=239000.0),
                   old_quart_fill=237000.0, new_quart_fill=239000.0, fee_rub=4.0)
    assert e.position.quart_entry == 238000.0 and e.position.rolled == 1


def test_session_persist_and_registry(tmp_path, monkeypatch):
    """Реестр без SBERF (сигнал не работает); позиция/журнал переживают рестарт."""
    from app.st7.service import ST7_PAIRS, St7Session
    assert "sberf" not in ST7_PAIRS
    assert ST7_PAIRS["imoexf"][3] == 20 and ST7_PAIRS["imoexf"][4] == 1
    s = St7Session()
    s._session_file = tmp_path / "s7.json"
    eng = _eng()
    s.engines["imoexf"] = eng
    eng.confirm_enter(_snap(), perp_fill=2344.0, quart_fill=236000.0, fee_rub=42.0)
    eng.position.perp_secid = "IMOEXF"
    s.trades.append({"pair": "imoexf", "net_pnl_rub": 500.0})
    s.save_session()
    s2 = St7Session()
    s2._session_file = tmp_path / "s7.json"
    monkeypatch.setattr(s2, "_engine", lambda pid: s2.engines.setdefault(pid, _eng()))
    assert s2.load_session() is True
    p = s2.engines["imoexf"].position
    assert p is not None and p.perp_lots == 20 and p.quart_lots == 1
    assert s2.trades[0]["net_pnl_rub"] == 500.0
