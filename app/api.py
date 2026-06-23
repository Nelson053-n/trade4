"""trade4 — самостоятельный backend стратегии st4 (спред-арбитраж SBRF/SBPR, SNGR/SNGP,
RTKM/RTKMP) на FORTS. Выделен из общего проекта trade в отдельный сервис: только движок st4,
свой премиум-дашборд, свой systemd/nginx (trade4.bananagen.ru). Phase 1 — paper / T-Bank sandbox.

Эндпоинты только /st4/* (см. app/st4/README.md). Панель (dashboard.html) отдаётся на «/».
Запуск:  uvicorn app.api:app  (из корня проекта).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime as _dt
from datetime import timedelta as _td
from datetime import timezone as _tz
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .st4 import data_feed as feed
from .st4.backtest import band_frame_for_chart, run_backtest
from .st4.service import ST4_PAIRS, St4Session, _clean

_BASE = Path(__file__).resolve().parent.parent          # корень проекта trade4
DASHBOARD = _BASE / "dashboard.html"
LOGIN_PAGE = _BASE / "login.html"
_MSK = _tz(_td(hours=3))                                 # московское время для меток

# ====================== авторизация (логин/пароль + подписанная cookie) ======================
# Один пользователь из окружения. Авторизация ВКЛЮЧАЕТСЯ только когда заданы оба
# TRADE4_USER и TRADE4_PASS (на проде — через systemd drop-in). Без них (локальная
# разработка, тесты) AUTH_ENABLED=False и middleware пропускает всё.
_AUTH_USER = os.environ.get("TRADE4_USER", "")
_AUTH_PASS = os.environ.get("TRADE4_PASS", "")
AUTH_ENABLED = bool(_AUTH_USER and _AUTH_PASS)
# Секрет подписи cookie: явный TRADE4_SECRET либо дериват от пароля (смена пароля
# разлогинивает все сессии — это нормально).
_AUTH_SECRET = (os.environ.get("TRADE4_SECRET", "")
                or hashlib.sha256(("trade4-cookie-v1|" + _AUTH_PASS).encode()).hexdigest())
_COOKIE_NAME = "trade4_session"
_SESSION_TTL = 7 * 86400                                 # 7 дней
# пути, доступные без авторизации
_AUTH_WHITELIST = {"/login", "/logout", "/login.html", "/health", "/favicon.ico", "/favicon.svg"}


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload: str) -> str:
    """payload + '.' + base64url(HMAC-SHA256(secret, payload)). Подписанный токен сессии."""
    sig = hmac.new(_AUTH_SECRET.encode(), payload.encode(), hashlib.sha256).digest()
    return _b64u(payload.encode()) + "." + _b64u(sig)


def _make_session() -> str:
    payload = f"{_AUTH_USER}|{int(time.time()) + _SESSION_TTL}"
    return _sign(payload)


def _verify(token: str) -> bool:
    """Проверить подпись и срок токена. Постоянное время сравнения подписи."""
    try:
        raw, sig = token.split(".", 1)
        payload = _b64u_dec(raw).decode()
        expected = _sign(payload).split(".", 1)[1]
        if not hmac.compare_digest(sig, expected):
            return False
        user, exp = payload.rsplit("|", 1)
        return user == _AUTH_USER and int(exp) > int(time.time())
    except Exception:
        return False

# независимая форвард-тест сессия на каждую пару обычка/преф (?pair=, см. ST4_PAIRS)
ST4S: dict[str, St4Session] = {p: St4Session(p) for p in ST4_PAIRS}

_server_started = 0.0


def _st4(pair: str = "sber") -> St4Session:
    if pair not in ST4S:
        raise HTTPException(400, "pair: " + " | ".join(ST4S))
    return ST4S[pair]


async def _st4_autoresume(ST4: St4Session):
    """Автостарт live после рестарта сервера: ПРОДОЛЖАЕМ сессию без сброса журнала.

    Paper: восстановленный движок продолжает (BB прогревается в run_live по last_live_ts).
    Sandbox: исполнителю нужен счёт — полный старт через reset_engine, журнал переносим.
    """
    ST4.state["player"] = False
    ST4.state["data_source"] = "live"
    ST4.state["live"] = True
    ST4.log_event("info", "автовозобновление live после рестарта сервера")
    if ST4.cfg.connector.mode == "tbank_sandbox":
        prev = ST4.engine                       # восстановленная сессия (журнал/баланс)
        started = ST4.state["session_started"]
        await asyncio.to_thread(ST4.reset_engine, True)
        eng = ST4.engine
        eng.trades = prev.trades
        eng.balance_rub = prev.balance_rub
        eng.risk.day_pnl_rub = prev.risk.day_pnl_rub
        eng.risk._day = prev.risk._day
        ST4.state["session_started"] = started
        ST4.save_session()
    if ST4.state["live"]:
        await ST4.run_live()


def _run_backtest_tbank(stop_sigma: float | None, ST4: St4Session) -> dict:
    """Прогон бэктеста на T-Bank-свечах + запись в историю. Блокирующий (to_thread)."""
    from .st4 import tbank_sandbox as _sb
    if not _sb.has_token():
        return {"error": "нужен токен T-Bank (вставьте в блоке «Коннектор»)"}
    try:
        spec_ord, spec_pref = feed.resolve_legs(ST4.cfg)
        it_o = _sb.find_future(spec_ord.code)
        it_p = _sb.find_future(spec_pref.code)
    except Exception as e:  # noqa: BLE001
        return {"error": f"T-Bank: {e}"}
    try:
        df = feed.read_ohlcv_tbank(ST4.cfg, 1000, _sb._uid(it_o), _sb._uid(it_p))
    except Exception as e:  # noqa: BLE001
        return {"error": f"не удалось получить свечи T-Bank: {e}"}
    if len(df) < ST4.cfg.strategy.sma_period + 20:
        return {"error": f"мало данных: {len(df)} баров (нужно > {ST4.cfg.strategy.sma_period})."}
    from .st4.config import St4Config as _Cfg
    from .st4.service import bt_history_append
    bt_cfg = _Cfg(**ST4.cfg.model_dump())
    if stop_sigma is not None:
        bt_cfg.strategy.stop_sigma = stop_sigma
    res = run_backtest(df, bt_cfg, spec_ord, spec_pref)
    res["legs"] = {"ord": spec_ord.code, "pref": spec_pref.code}
    res["source"] = "T-Bank real-time"
    entry = {
        "date": _dt.now(_MSK).strftime("%Y-%m-%d %H:%M"),
        "stop_sigma": stop_sigma if stop_sigma is not None else ST4.cfg.strategy.stop_sigma,
        "bars": res["bars"], "trades": res["trades"], "win_rate_pct": res["win_rate_pct"],
        "net_pnl_rub": res["net_pnl_rub"], "return_pct": res["return_pct"],
        "max_drawdown_pct": res["max_drawdown_pct"], "stops": res["stops"],
    }
    res["history"] = bt_history_append(entry, pair=ST4.pair)
    return res


async def _auto_backtest_loop():
    """Авто-бэктест раз в ~2.5 дня: копит историю результативности на свежих данных T-Bank."""
    await asyncio.sleep(120)                 # первый прогон — через 2 мин после старта
    while True:
        try:
            from .st4 import tbank_sandbox as _sb
            if _sb.has_token():
                for s4 in ST4S.values():
                    res = await asyncio.to_thread(_run_backtest_tbank, None, s4)
                    if "error" not in res:
                        s4.log_event("info", f"авто-бэктест T-Bank: сделок {res['trades']}, "
                                     f"net {res['net_pnl_rub']:+.0f}₽, win {res['win_rate_pct']}%")
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(2.5 * 24 * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import time as _time
    global _server_started
    _server_started = _time.time()
    from .st4 import tbank_sandbox as _sb
    _sb.load_token()                  # подтянуть сохранённый токен T-Bank (переживает рестарт)
    for s4 in ST4S.values():
        s4.load_session()
        if s4.state.pop("resume_live", False):
            asyncio.create_task(_st4_autoresume(s4))   # автостарт: live шёл до рестарта
    _auto_bt_task = asyncio.create_task(_auto_backtest_loop())
    yield
    _auto_bt_task.cancel()
    for s4 in ST4S.values():
        s4.save_session()
        s4.state["live"] = False
        s4.state["player"] = False


app = FastAPI(title="trade4 — st4 spread arbitrage", version="1.0", lifespan=lifespan)


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    """Закрывает весь сервис за авторизацией (safe-by-default). Без валидной сессии
    «/» отдаёт страницу логина, остальное — 401. Whitelist: логин/логаут/health."""
    if not AUTH_ENABLED or request.url.path in _AUTH_WHITELIST:
        return await call_next(request)
    if _verify(request.cookies.get(_COOKIE_NAME, "")):
        return await call_next(request)
    if request.url.path == "/":
        if LOGIN_PAGE.exists():
            return FileResponse(LOGIN_PAGE, headers={"Cache-Control": "no-cache"})
        return JSONResponse({"detail": "login.html не найден"}, status_code=500)
    return JSONResponse({"detail": "не авторизован"}, status_code=401)


@app.post("/login")
async def login(request: Request):
    """Вход по логину/паролю (form-data или JSON). Успех → подписанная HttpOnly cookie."""
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        data = await request.json()
    else:
        data = dict(await request.form())
    user = str(data.get("username", ""))
    pw = str(data.get("password", ""))
    ok = (hmac.compare_digest(user, _AUTH_USER)
          and hmac.compare_digest(pw, _AUTH_PASS)) if AUTH_ENABLED else True
    if not ok:
        return JSONResponse({"detail": "неверный логин или пароль"}, status_code=401)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(_COOKIE_NAME, _make_session(), max_age=_SESSION_TTL,
                    httponly=True, samesite="lax", secure=True, path="/")
    return resp


@app.post("/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_COOKIE_NAME, path="/")
    return resp


@app.get("/login.html")
def login_page():
    if not LOGIN_PAGE.exists():
        raise HTTPException(404, "login.html не найден")
    return FileResponse(LOGIN_PAGE, headers={"Cache-Control": "no-cache"})


@app.get("/favicon.svg")
def favicon():
    f = _BASE / "favicon.svg"
    if not f.exists():
        raise HTTPException(404)
    return FileResponse(f, media_type="image/svg+xml",
                        headers={"Cache-Control": "max-age=86400"})


@app.get("/")
def dashboard():
    if not DASHBOARD.exists():
        raise HTTPException(404, "dashboard.html не найден")
    # no-cache: панель часто обновляется; иначе браузер по ETag отдаёт старую версию из кэша
    return FileResponse(DASHBOARD, headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/health")
def health():
    return {"ok": True, "pairs": [{"id": p, "live": s.state["live"], "player": s.state["player"]}
                                  for p, s in ST4S.items()]}


# ============================================================================
# st4 — арбитраж спреда фьючерсов обычка/преф (FORTS, MOEX ISS). FSM-движок,
# paper/sandbox-исполнение с атомарностью пар. Сессия на пару (St4Session).
# ============================================================================
ST4_REPORT_HTML = _BASE / "st4_report.html"
_ST4_SCAN = {"running": False, "error": None}


@app.get("/st4/pairs")
def st4_pairs():
    """Список доступных пар обычка/преф — фронт строит переключатель динамически."""
    return {"pairs": [{"id": pid, "ord": spec[0], "pref": spec[1], "label": spec[2]}
                      for pid, spec in ST4_PAIRS.items()]}


@app.get("/st4/state")
def st4_state(pair: str = "sber"):
    return _st4(pair).snapshot(_server_started)


@app.get("/st4/config")
def st4_config(pair: str = "sber"):
    return _st4(pair).cfg.model_dump()


@app.post("/st4/config")
async def st4_set_config(payload: dict, pair: str = "sber"):
    """Обновить параметры стратегии/риска/исполнения (валидация) и сбросить сессию.

    Если до применения был активен live/демо — автоматически перезапускаем его с новыми
    параметрами (чтобы не нажимать «live» вручную после каждого изменения)."""
    ST4 = _st4(pair)
    s = ST4.cfg.strategy
    r = ST4.cfg.risk
    e = ST4.cfg.execution

    def _num(key, lo, hi, cur):
        if key not in payload or payload[key] is None:
            return cur
        try:
            v = float(payload[key])
        except (TypeError, ValueError):
            raise HTTPException(400, f"{key}: не число")
        if not (lo <= v <= hi):
            raise HTTPException(400, f"{key}: вне диапазона [{lo}, {hi}]")
        return v

    s.sma_period = int(_num("sma_period", 20, 1000, s.sma_period))
    s.sigma_multiplier = _num("sigma_multiplier", 0.5, 5.0, s.sigma_multiplier)
    s.deviation_pct = _num("deviation_pct", 0.0, 0.2, s.deviation_pct)
    s.stop_sigma = _num("stop_sigma", 0.0, 10.0, s.stop_sigma)
    s.max_bars_in_trade = int(_num("max_bars_in_trade", 0, 100000, s.max_bars_in_trade))
    s.deviation_sigma = _num("deviation_sigma", 0.0, 10.0, s.deviation_sigma)
    s.pending_ttl_bars = int(_num("pending_ttl_bars", 1, 100, s.pending_ttl_bars))
    if "deviation_mode" in payload:
        if payload["deviation_mode"] not in ("AbsOfMean", "LiteralPct", "Sigma"):
            raise HTTPException(400, "deviation_mode: AbsOfMean | LiteralPct | Sigma")
        s.deviation_mode = payload["deviation_mode"]
    if "entry_trigger" in payload:
        if payload["entry_trigger"] not in ("Breakout", "ReEntry"):
            raise HTTPException(400, "entry_trigger: Breakout | ReEntry")
        s.entry_trigger = payload["entry_trigger"]
    if "freeze_sma_on_exit" in payload:
        s.freeze_sma_on_exit = bool(payload["freeze_sma_on_exit"])
    if "interval_min" in payload:
        iv = int(payload["interval_min"])
        if iv not in (1, 10, 60):
            raise HTTPException(400, "interval_min: 1 | 10 | 60 (ISS не отдаёт 5m)")
        s.candle_interval_minutes = iv
    r.max_daily_loss_rub = _num("max_daily_loss_rub", 0, 1e9, r.max_daily_loss_rub)
    e.quantity_lots = int(_num("quantity_lots", 1, 1000, e.quantity_lots))
    if "auto_approve" in payload:
        ST4.cfg.auto_approve = bool(payload["auto_approve"])

    was_live, was_player = ST4.state["live"], ST4.state["player"]
    ST4.state["live"] = ST4.state["player"] = False
    if was_live:
        ST4.state["live"] = True
        ST4.log_event("info", "параметры применены — перезапуск live в фоне")

        async def _boot():
            await asyncio.to_thread(ST4.reset_engine, True)
            if ST4.state["live"]:
                await ST4.run_live()

        asyncio.create_task(_boot())
    elif was_player:
        ST4.reset_engine(real=False)
        ST4.state["player"] = True
        ST4.player_df = feed.generate_synthetic(
            n=1500, interval_min=ST4.cfg.strategy.candle_interval_minutes)
        ST4.player_idx = 0
        asyncio.create_task(ST4.run_player())
    else:
        ST4.reset_engine(real=(ST4.state["data_source"] == "live"))
    return {"ok": True, "config": ST4.cfg.model_dump(), "was_live": was_live,
            "was_player": was_player, "restarted": was_live or was_player}


@app.post("/st4/control/start")
async def st4_start(pair: str = "sber"):
    """Запустить live. Тяжёлый старт (резолв серий + sandbox-счёт + pay_in) — в фоне."""
    ST4 = _st4(pair)
    if ST4.state["live"]:
        return {"ok": True, "already": True}
    ST4.state["player"] = False
    ST4.state["data_source"] = "live"
    ST4.state["live"] = True
    ST4.state["paused_by_user"] = False  # старт снимает намеренную остановку
    ST4.log_event("info", "запуск live… (резолв инструментов и счёта в фоне)")

    async def _boot():
        await asyncio.to_thread(ST4.reset_engine, True)   # сеть — в пуле потоков
        if ST4.state["live"]:                              # не отменили за время старта
            await ST4.run_live()

    asyncio.create_task(_boot())
    return {"ok": True, "mode": "live", "starting": True}


@app.post("/st4/control/stop")
def st4_stop(pair: str = "sber"):
    ST4 = _st4(pair)
    ST4.state["live"] = False
    ST4.state["paused_by_user"] = True   # намеренная остановка — автостарт не возобновляет
    ST4.save_session()
    return {"ok": True}


@app.post("/st4/player/start")
async def st4_player_start(limit: int = 1500, pair: str = "sber"):
    """Запустить/возобновить синтетический плеер (офлайн-демо FSM)."""
    ST4 = _st4(pair)
    if ST4.state["player"]:
        return {"ok": True, "already": True}
    ST4.state["live"] = False
    ST4.state["paused_by_user"] = False
    ST4.state["data_source"] = "synthetic"
    resuming = ST4.player_df is not None and ST4.player_idx < len(ST4.player_df)
    if not resuming:
        ST4.reset_engine(real=False)
        iv = ST4.cfg.strategy.candle_interval_minutes
        ST4.player_df = feed.generate_synthetic(n=limit, interval_min=iv)
        ST4.player_idx = 0
    ST4.state["player"] = True
    ST4.save_session()
    asyncio.create_task(ST4.run_player())
    return {"ok": True, "resumed": resuming}


@app.post("/st4/player/stop")
def st4_player_stop(pair: str = "sber"):
    ST4 = _st4(pair)
    ST4.state["player"] = False
    ST4.state["paused_by_user"] = True
    return {"ok": True}


@app.post("/st4/control/flat-all")
def st4_flat_all(payload: dict | None = None, pair: str = "sber"):
    """Паник-закрытие позиции по рынку (требует confirm=true в теле)."""
    ST4 = _st4(pair)
    if not payload or not payload.get("confirm"):
        raise HTTPException(400, "нужно подтверждение: {\"confirm\": true}")
    trade = ST4.engine.flat_all("flat_all")
    ST4.save_session()
    return {"ok": True, "closed": trade is not None,
            "net_pnl_rub": round(trade.net_pnl_rub, 0) if trade else None}


@app.post("/st4/control/trading")
def st4_trading(on: bool = True, pair: str = "sber"):
    """Глобальный флаг новых входов (TradingEnabled)."""
    ST4 = _st4(pair)
    ST4.cfg.risk.trading_enabled = on
    return {"ok": True, "trading_enabled": on}


@app.post("/st4/control/resume")
def st4_resume(pair: str = "sber"):
    """Снять HALTED после ручного разбора."""
    ST4 = _st4(pair)
    ST4.engine.risk.resume()
    from .st4.models import BotState
    if ST4.engine.state == BotState.HALTED:
        ST4.engine.state = BotState.FLAT
    return {"ok": True, "halted": ST4.engine.risk.halted}


@app.post("/st4/approve")
def st4_approve(pair: str = "sber"):
    ST4 = _st4(pair)
    if ST4.engine._pending is None:
        raise HTTPException(400, "нет ожидающей рекомендации")
    ST4.engine.approve()
    ST4.save_session()
    return {"ok": True}


@app.post("/st4/reject")
def st4_reject(pair: str = "sber"):
    ST4 = _st4(pair)
    ST4.engine.reject()
    return {"ok": True}


@app.post("/st4/auto")
def st4_auto(on: bool = True, pair: str = "sber"):
    ST4 = _st4(pair)
    ST4.cfg.auto_approve = on
    if on and ST4.engine._pending is not None:
        ST4.engine.approve()
        ST4.save_session()
    return {"ok": True, "auto_approve": on}


@app.post("/st4/reset")
def st4_reset(pair: str = "sber"):
    ST4 = _st4(pair)
    ST4.reset_engine(real=(ST4.state["data_source"] == "live"))
    return {"ok": True}


@app.post("/st4/reload-params")
async def st4_reload_params(payload: dict | None = None, pair: str = "sber"):
    """Горячо переприменить параметры стратегии ОДНОЙ пары — без рестарта сервиса и без
    потери открытой позиции. Другие пары не затрагиваются. История/BB прогреются заново."""
    ST4 = _st4(pair)
    res = await asyncio.to_thread(ST4.apply_pair_params, payload or None)
    return {"ok": True, "pair": pair, **res}


@app.post("/st4/restore-position")
def st4_restore_position(payload: dict, pair: str = "sber"):
    """Восстановить открытую позицию в ЖИВОМ движке (без файловой гонки)."""
    from .st4.models import BotState, LegPosition, Position, Role
    ST4 = _st4(pair)
    try:
        state = BotState(payload["state"])
        if state not in (BotState.LONG_SPREAD, BotState.SHORT_SPREAD):
            raise HTTPException(400, "state: long_spread | short_spread")
        lots = int(payload["lots"])
        ord_side = "buy" if state == BotState.LONG_SPREAD else "sell"
        pref_side = "sell" if state == BotState.LONG_SPREAD else "buy"
        ord_entry = float(payload["ord_entry"])
        pref_entry = float(payload["pref_entry"])
        leg_ord = LegPosition(code=payload["ord_code"], role=Role.ORDINARY, side=ord_side,
                              lots=lots, entry_price=ord_entry)
        leg_pref = LegPosition(code=payload["pref_code"], role=Role.PREFERRED, side=pref_side,
                               lots=lots, entry_price=pref_entry)
        ST4.engine.position = Position(
            state=state, leg_ord=leg_ord, leg_pref=leg_pref,
            entry_ts=int(payload.get("entry_ts", 0)),
            entry_spread=pref_entry - ord_entry, entry_beta=1.0,
            sma_at_entry=float(payload.get("sma_at_entry", pref_entry - ord_entry)),
            entry_fee_rub=float(payload.get("entry_fee_rub", 0.0)))
        ST4.engine.state = state
        ST4.engine._bars_held = int(payload.get("bars_held", 0))
        ST4.save_session()
        return {"ok": True, "position": ST4.engine.state.value, "lots": lots}
    except KeyError as e:
        raise HTTPException(400, f"нет поля: {e}")


@app.post("/st4/connector")
def st4_connector(payload: dict, pair: str = "sber"):
    """Установить режим исполнителя (paper|tbank_sandbox) и (опц.) API-токен T-Bank.

    Токен сохраняется в файл .tbank_token (0600, в .gitignore) и в env процесса. В ответе
    НЕ возвращается. Sandbox активен только в live; при недоступности reset откатывает в paper."""
    from .st4 import tbank_sandbox as _sb

    ST4 = _st4(pair)
    mode = payload.get("mode")
    if mode not in ("paper", "tbank_sandbox"):
        raise HTTPException(400, "mode: paper | tbank_sandbox")
    token = (payload.get("token") or "").strip()
    if token:
        _sb.save_token(token)
    if mode == "tbank_sandbox" and not _sb.has_token():
        raise HTTPException(400, "для sandbox нужен токен (вставьте в поле API-токен)")
    if "payin_rub" in payload:
        try:
            ST4.cfg.connector.payin_rub = int(payload["payin_rub"])
        except (TypeError, ValueError):
            raise HTTPException(400, "payin_rub: не число")
    ST4.cfg.connector.mode = mode
    ST4.state["live"] = ST4.state["player"] = False
    ST4.reset_engine(real=(ST4.state["data_source"] == "live"))
    return {"ok": True, "connector_mode": ST4.cfg.connector.mode,
            "token_set": _sb.has_token(),
            "fell_back": ST4.cfg.connector.mode != mode}


@app.post("/st4/connector/forget-token")
def st4_forget_token():
    """Удалить сохранённый токен (из файла и env). Откатить в paper ВСЕ пары (токен общий)."""
    from .st4 import tbank_sandbox as _sb
    _sb.save_token("")
    for s4 in ST4S.values():
        if s4.cfg.connector.mode != "paper":
            s4.cfg.connector.mode = "paper"
            s4.reset_engine(real=(s4.state["data_source"] == "live"))
    return {"ok": True, "token_set": False, "connector_mode": "paper"}


@app.get("/st4/trades")
def st4_trades(pair: str = "sber"):
    ST4 = _st4(pair)
    return {"trades": [ST4._trade_json(t) for t in ST4.engine.trades]}


@app.get("/st4/daily")
def st4_daily(pair: str = "sber"):
    """Доходность по дням: дата (МСК), net P&L за день (₽) и число сделок в день."""
    ST4 = _st4(pair)
    by_day: dict[str, dict] = {}
    for t in ST4.engine.trades:
        d = _dt.fromtimestamp(t.exit_ts / 1000, tz=_MSK).strftime("%Y-%m-%d")
        e = by_day.setdefault(d, {"date": d, "net_pnl_rub": 0.0, "trades": 0, "wins": 0})
        e["net_pnl_rub"] += t.net_pnl_rub
        e["trades"] += 1
        if t.net_pnl_rub > 0:
            e["wins"] += 1
    days = sorted(by_day.values(), key=lambda x: x["date"])
    cum = 0.0
    for d in days:
        cum += d["net_pnl_rub"]
        d["net_pnl_rub"] = round(d["net_pnl_rub"], 0)
        d["cum_pnl_rub"] = round(cum, 0)
    return _clean({"pair": pair, "days": days})


@app.get("/st4/backtest")
async def st4_backtest(days: int = 90, stop_sigma: float | None = None, pair: str = "sber"):
    """Бэктест на реальной истории MOEX ISS за period (honest: maxDD по equity)."""
    ST4 = _st4(pair)

    def _run() -> dict:
        try:
            spec_ord, spec_pref = feed.resolve_legs(ST4.cfg)
        except Exception as e:  # noqa: BLE001
            return {"error": f"не удалось определить серии: {e}"}
        since = _dt.now(_tz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        since = since.fromtimestamp(since.timestamp() - days * 86400, tz=_tz.utc)
        try:
            df = feed.read_ohlcv_moex_range(ST4.cfg, since, spec_ord.code, spec_pref.code)
        except Exception as e:  # noqa: BLE001
            return {"error": f"не удалось получить историю: {e}"}
        if len(df) < ST4.cfg.strategy.sma_period + 20:
            return {"error": f"мало данных: {len(df)} баров (нужно > {ST4.cfg.strategy.sma_period})"}
        from .st4.config import St4Config as _Cfg
        bt_cfg = _Cfg(**ST4.cfg.model_dump())
        if stop_sigma is not None:
            bt_cfg.strategy.stop_sigma = stop_sigma
        res = run_backtest(df, bt_cfg, spec_ord, spec_pref)
        res["bands"] = band_frame_for_chart(df, bt_cfg)[-400:]
        res["legs"] = {"ord": spec_ord.code, "pref": spec_pref.code}
        from .st4.service import bt_history_append
        entry = {
            "date": _dt.now(_MSK).strftime("%Y-%m-%d %H:%M"),
            "days": days,
            "stop_sigma": stop_sigma if stop_sigma is not None else ST4.cfg.strategy.stop_sigma,
            "bars": res["bars"], "trades": res["trades"], "win_rate_pct": res["win_rate_pct"],
            "net_pnl_rub": res["net_pnl_rub"], "return_pct": res["return_pct"],
            "max_drawdown_pct": res["max_drawdown_pct"], "stops": res["stops"],
        }
        res["history"] = bt_history_append(entry, source="moex", pair=ST4.pair)
        return res

    return _clean(await asyncio.to_thread(_run))


@app.get("/st4/backtest_tbank")
async def st4_backtest_tbank(stop_sigma: float | None = None, pair: str = "sber"):
    """Бэктест на РЕАЛЬНЫХ котировках T-Bank за неделю (тот же источник, что sandbox-ордера)."""
    return _clean(await asyncio.to_thread(_run_backtest_tbank, stop_sigma, _st4(pair)))


@app.get("/st4/backtest_history")
def st4_backtest_history(source: str = "tbank", pair: str = "sber"):
    """История прогонов бэктеста (source: tbank | moex) — результативность во времени."""
    from .st4.service import bt_history_load
    if source not in ("tbank", "moex"):
        raise HTTPException(400, "source: tbank | moex")
    return {"history": bt_history_load(source, _st4(pair).pair)}


@app.get("/st4/report")
def st4_report_page():
    """Страница отчёта скана пар (ссылка с панели)."""
    if not ST4_REPORT_HTML.exists():
        raise HTTPException(404, "st4_report.html не найден")
    return FileResponse(ST4_REPORT_HTML)


@app.get("/st4/scan/report")
def st4_scan_report():
    """Последний результат скана пар + статус текущего прогона."""
    from .st4.scan_pairs import OUT_JSON
    rep = None
    if OUT_JSON.exists():
        try:
            rep = json.loads(OUT_JSON.read_text())
        except Exception:  # noqa: BLE001
            rep = None
    return {"report": rep, "running": _ST4_SCAN["running"], "error": _ST4_SCAN["error"]}


@app.post("/st4/scan/run")
async def st4_scan_run(days: int = 60, stop_sigma: float | None = None, pair: str = "sber"):
    """Запустить скан в фоне (ISS медленный — минуты). Параметры стратегии — текущие st4."""
    ST4 = _st4(pair)
    if _ST4_SCAN["running"]:
        return {"ok": True, "already": True}
    _ST4_SCAN["running"] = True
    _ST4_SCAN["error"] = None

    async def _job():
        try:
            from .st4.scan_pairs import run_scan
            rep = await asyncio.to_thread(run_scan, days, stop_sigma, None, ST4.cfg)
            ok = sum(1 for r in rep["rows"] if "error" not in r)
            ST4.log_event("info", f"скан пар FORTS завершён: {ok}/{len(rep['rows'])} пар, {days}д")
        except Exception as e:  # noqa: BLE001
            _ST4_SCAN["error"] = str(e)
        finally:
            _ST4_SCAN["running"] = False

    asyncio.create_task(_job())
    return {"ok": True, "started": True}


@app.get("/st4/margin")
async def st4_margin(pair: str = "sber"):
    """Гарантийное обеспечение пары и расчёт для 1/5/10/100 контрактов (INITIALMARGIN из ISS)."""
    ST4 = _st4(pair)

    def _run() -> dict:
        try:
            ST4.resolve_real_legs()
            m_ord = feed.leg_margin(ST4.spec_ord.code)
            m_pref = feed.leg_margin(ST4.spec_pref.code)
        except Exception as e:  # noqa: BLE001
            return {"error": f"не удалось получить ГО: {e}"}
        pair_m = m_ord + m_pref
        balance = None
        try:
            from .st4 import tbank_sandbox as _sb
            if _sb.has_token() and ST4.cfg.connector.account_id:
                pf = _sb.portfolio(ST4.cfg.connector.account_id)
                q = pf.get("totalAmountPortfolio")
                balance = (int(q.get("units", 0)) + int(q.get("nano", 0)) / 1e9) if q else None
        except Exception:  # noqa: BLE001
            pass
        rows = []
        for n in (1, 5, 10, 100):
            margin = round(pair_m * n)
            rows.append({
                "lots": n, "margin_rub": margin,
                "pct_of_balance": round(100 * margin / balance, 1) if balance else None,
            })
        return {
            "legs": {"ord": ST4.spec_ord.code, "pref": ST4.spec_pref.code},
            "margin_ord": round(m_ord), "margin_pref": round(m_pref),
            "margin_pair": round(pair_m),
            "balance_rub": round(balance) if balance else None,
            "rows": rows,
        }

    return _clean(await asyncio.to_thread(_run))


@app.get("/st4/tests")
async def st4_tests():
    """Статус юнит-тестов st4."""
    import re
    import subprocess
    import sys

    def _run() -> dict:
        try:
            p = subprocess.run(
                [sys.executable, "-m", "pytest", "tests/test_st4.py",
                 "--no-header", "-p", "no:cacheprovider"],
                cwd=str(_BASE), capture_output=True, text=True, timeout=180)
            out = p.stdout + p.stderr
            passed = int(m.group(1)) if (m := re.search(r"(\d+) passed", out)) else 0
            failed = int(m.group(1)) if (m := re.search(r"(\d+) failed", out)) else 0
            return {"passed": passed, "failed": failed, "ok": failed == 0 and passed > 0,
                    "tail": out.strip().splitlines()[-1:] if out else []}
        except Exception as e:  # noqa: BLE001
            return {"passed": 0, "failed": 0, "ok": False, "error": str(e)}

    return await asyncio.to_thread(_run)
