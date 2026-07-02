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
from .st5.service import St5Session

_BASE = Path(__file__).resolve().parent.parent          # корень проекта trade4
DASHBOARD = _BASE / "dashboard.html"
LOGIN_PAGE = _BASE / "login.html"
_MSK = _tz(_td(hours=3))                                 # московское время для меток

# ====================== авторизация (логин/пароль + подписанная cookie) ======================
# Один пользователь из окружения. Авторизация ВКЛЮЧАЕТСЯ только когда заданы оба
# TRADE4_USER и TRADE4_PASS (на проде — через systemd drop-in).
_AUTH_USER = os.environ.get("TRADE4_USER", "")
_AUTH_PASS = os.environ.get("TRADE4_PASS", "")
AUTH_ENABLED = bool(_AUTH_USER and _AUTH_PASS)
# H1 (fail-closed): без заданных кредов сервис НЕ открывается анонимам (боевой контур!).
# Локальная разработка/тесты должны выставить TRADE4_ALLOW_NOAUTH=1 явно — тогда middleware
# пропускает всё. Иначе при пустых кредах всё, кроме whitelist, отдаёт 503.
_ALLOW_NOAUTH = os.environ.get("TRADE4_ALLOW_NOAUTH", "") == "1"
# H2: секрет подписи cookie — ТРЕБУЕМ явный TRADE4_SECRET. Фолбэк-дериват от пароля оставлен
# для совместимости (смена пароля разлогинивает сессии), но это слабее — при старте логируем
# предупреждение (см. lifespan), чтобы на проде задавали отдельный TRADE4_SECRET ≥32 байт.
_AUTH_SECRET = (os.environ.get("TRADE4_SECRET", "")
                or hashlib.sha256(("trade4-cookie-v1|" + _AUTH_PASS).encode()).hexdigest())
_SECRET_IS_DERIVED = not os.environ.get("TRADE4_SECRET", "")
_COOKIE_NAME = "trade4_session"
_SESSION_TTL = 24 * 3600                                 # H2: 24ч (было 7 дней)
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

# ST5 — ОДНА портфельная сессия на весь портфель (до 3 позиций на разные пары)
ST5 = St5Session()

_server_started = 0.0


def _st4(pair: str = "sber") -> St4Session:
    if pair not in ST4S:
        raise HTTPException(400, "pair: " + " | ".join(ST4S))
    return ST4S[pair]


def _guard_no_position(s4: St4Session, action: str) -> None:
    """Запретить опасное действие при ОТКРЫТОЙ позиции (смена параметров/пауза/сброс
    пересоздают движок или рвут сессию → рассинхрон с реальной позицией на счёте).
    409 с понятным текстом — UI показывает уведомление, бэкенд защищён независимо от UI."""
    if s4.engine.position is not None:
        raise HTTPException(409, f"активная позиция — {action} невозможно. Сначала закройте "
                                 "позицию (flat-all) или дождитесь выхода по стратегии.")


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


async def _st5_autoresume():
    """Автостарт ST5 live после рестарта: восстановить sandbox-режим и продолжить торговать.

    Коннектор (mode/account) и sandbox_active восстановлены из session-файла в load_session.
    Журнал/история/last_live_ts тоже восстановлены — движок прогреется по last_live_ts."""
    import time as _t
    ST5.state["live"] = True
    ST5.state["live_intent"] = True    # сохраняем намерение → цепочка автостартов не прервётся
    ST5.state["paused_by_user"] = False
    ST5.state["data_source"] = "live"
    if not ST5.state.get("session_started"):
        ST5.state["session_started"] = _t.time()
    # пересобрать sandbox_active по восстановленному коннектору (на случай рассогласования)
    want_broker = ST5.cfg.connector.mode in ("tbank_sandbox", "tbank_real")
    ST5.state["sandbox_active"] = bool(want_broker and ST5.cfg.connector.account_id)
    ST5.log_event("info", f"автовозобновление ST5 live после рестарта ({ST5.cfg.connector.mode})")
    if ST5.state["live"]:
        ST5.start_live()


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
    # предупреждения о конфигурации безопасности (H1/H2)
    if not AUTH_ENABLED:
        if _ALLOW_NOAUTH:
            print("⚠️  trade4: запуск БЕЗ авторизации (TRADE4_ALLOW_NOAUTH=1) — только для dev/тестов")
        else:
            print("⛔ trade4: TRADE4_USER/PASS не заданы — сервис fail-closed (503 на всё). "
                  "Задайте креды или TRADE4_ALLOW_NOAUTH=1 для dev")
    elif _SECRET_IS_DERIVED:
        print("⚠️  trade4: TRADE4_SECRET не задан — секрет cookie деривируется из пароля (слабее). "
              "Задайте отдельный TRADE4_SECRET ≥32 байт на проде")
    from .st4 import tbank_sandbox as _sb
    _sb.load_token()                  # подтянуть сохранённый токен T-Bank (переживает рестарт)
    from .st5 import notifier as _tg
    _tg.load_bot_token()              # подтянуть токен Telegram-бота (переживает рестарт)
    for s4 in ST4S.values():
        s4.load_session()
        if s4.state.pop("resume_live", False):
            asyncio.create_task(_st4_autoresume(s4))   # автостарт: live шёл до рестарта
    ST5.load_session()                                  # портфельная сессия st5
    if ST5.state.pop("resume_live", False):
        asyncio.create_task(_st5_autoresume())          # автостарт: ST5 live шёл до рестарта
    _watchdog_task = asyncio.create_task(ST5.watchdog_loop())  # самовосстановление зависшего live-цикла
    _auto_bt_task = asyncio.create_task(_auto_backtest_loop())
    yield
    _watchdog_task.cancel()
    _auto_bt_task.cancel()
    for s4 in ST4S.values():
        s4.save_session()
        s4.state["live"] = False
        s4.state["player"] = False
    ST5.state["live"] = False
    ST5.save_session()


app = FastAPI(title="trade4 — st4 spread arbitrage", version="1.0", lifespan=lifespan)


def _csrf_ok(request: Request) -> bool:
    """C2: для мутирующих запросов (POST/PUT/PATCH/DELETE) требуем same-origin.
    Проверяем Sec-Fetch-Site (современные браузеры) ИЛИ Origin против Host. Отсутствие обоих
    (curl/скрипты без заголовков) пропускаем — CSRF возможен только из браузера, где заголовки
    проставляются автоматически и их подделать кросс-сайтом нельзя."""
    sfs = request.headers.get("sec-fetch-site")
    if sfs is not None:
        return sfs in ("same-origin", "same-site", "none")
    origin = request.headers.get("origin")
    if origin:
        host = request.headers.get("host", "")
        return origin.split("://", 1)[-1] == host
    return True   # ни Sec-Fetch-Site, ни Origin — не браузерный кросс-сайт


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    """Закрывает весь сервис за авторизацией (safe-by-default, H1 fail-closed). Без валидной
    сессии «/» отдаёт логин, остальное — 401. Мутирующие запросы — CSRF-проверка (C2)."""
    path = request.url.path
    resp = await _route(request, call_next, path)
    _apply_security_headers(resp)   # M1: на ВСЕ ответы (включая 401/403/503/логин)
    return resp


async def _route(request: Request, call_next, path: str):
    if path in _AUTH_WHITELIST:
        return await call_next(request)
    if not AUTH_ENABLED:
        # H1: без кредов открыто ТОЛЬКО при явном dev-флаге, иначе fail-closed (503)
        if _ALLOW_NOAUTH:
            return await call_next(request)
        return JSONResponse({"detail": "сервис не сконфигурирован (нет TRADE4_USER/PASS)"},
                            status_code=503)
    if not _verify(request.cookies.get(_COOKIE_NAME, "")):
        if path == "/":
            if LOGIN_PAGE.exists():
                return FileResponse(LOGIN_PAGE, headers={"Cache-Control": "no-cache"})
            return JSONResponse({"detail": "login.html не найден"}, status_code=500)
        return JSONResponse({"detail": "не авторизован"}, status_code=401)
    # C2: авторизован — но мутирующие методы защищаем от CSRF (кросс-сайт POST с cookie)
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and not _csrf_ok(request):
        return JSONResponse({"detail": "CSRF: cross-origin запрос отклонён"}, status_code=403)
    return await call_next(request)


# M1: заголовки безопасности на уровне приложения (работают даже при прямом доступе к origin,
# не только через nginx). CSP разрешает inline-стили/скрипты — дашборд самодостаточный
# (один файл со встроенными <style>/<script>), плюс шрифты Google.
_SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'"),
}


def _apply_security_headers(resp) -> None:
    for k, v in _SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)


# H3: лёгкий in-process anti-bruteforce на /login — окно по IP. Не замена nginx limit_req,
# но закрывает базовый перебор пароля панели боевой торговли.
_LOGIN_FAILS: dict[str, list] = {}                      # ip -> [ts неудач в окне]
_LOGIN_WINDOW = 300                                     # окно 5 мин
_LOGIN_MAX_FAILS = 10                                   # >10 неудач за окно → 429


def _login_blocked(ip: str) -> bool:
    now = time.time()
    fails = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < _LOGIN_WINDOW]
    _LOGIN_FAILS[ip] = fails
    return len(fails) >= _LOGIN_MAX_FAILS


def _login_record_fail(ip: str) -> None:
    _LOGIN_FAILS.setdefault(ip, []).append(time.time())


@app.post("/login")
async def login(request: Request):
    """Вход по логину/паролю (form-data или JSON). Успех → подписанная HttpOnly cookie."""
    ip = request.client.host if request.client else "?"
    if _login_blocked(ip):
        return JSONResponse({"detail": "слишком много попыток, подождите"}, status_code=429)
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
        _login_record_fail(ip)
        return JSONResponse({"detail": "неверный логин или пароль"}, status_code=401)
    _LOGIN_FAILS.pop(ip, None)                           # успех — сбросить счётчик
    resp = JSONResponse({"ok": True})
    resp.set_cookie(_COOKIE_NAME, _make_session(), max_age=_SESSION_TTL,
                    httponly=True, samesite="strict", secure=True, path="/")  # H2: Strict
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


@app.get("/strategy.md")
def strategy_md():
    """Описание стратегии для анализа нейросетью (STRATEGY.md). Отдаётся как markdown."""
    f = _BASE / "STRATEGY.md"
    if not f.exists():
        raise HTTPException(404, "STRATEGY.md не найден")
    return FileResponse(f, media_type="text/markdown; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})


@app.get("/strategy5.md")
def strategy5_md():
    """Описание стратегии ST5 для анализа (STRATEGY5.md)."""
    f = _BASE / "STRATEGY5.md"
    if not f.exists():
        raise HTTPException(404, "STRATEGY5.md не найден")
    return FileResponse(f, media_type="text/markdown; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})


@app.get("/health")
def health():
    return {"ok": True, "pairs": [{"id": p, "live": s.state["live"], "player": s.state["player"]}
                                  for p, s in ST4S.items()]}


# ============================================================================
# st5 — institutional statarb (коинтеграция + Kalman β + z-score). ОДНА портфельная
# сессия (до 3 позиций на разные пары). Боевой контур реальными деньгами под защитами.
# ============================================================================
def _st5_guard_no_position(action: str) -> None:
    """Запрет опасных действий при ЛЮБОЙ открытой позиции портфеля st5."""
    busy = [pid for pid, e in ST5.engines.items() if e.position is not None]
    if busy:
        raise HTTPException(409, f"активные позиции ({', '.join(busy)}) — {action} невозможно. "
                                 "Сначала закройте позиции (flat-all).")


def _st5_pair_cfg(pair: str):
    """Конфиг пары с per-pair оверрайдами (как у её live-движка) — для бэктестов/sweep,
    чтобы результаты совпадали с реальной торговлей этой пары, а не с общим ST5.cfg."""
    return ST5.pair_cfgs.get(pair) or ST5._pair_cfg(pair)


_ST5_BT_HISTORY: list[dict] = []   # журнал прошлых бэктест-прогонов (последние 40)


def _st5_bt_log(entry: dict) -> None:
    entry["ts"] = _dt.now(_MSK).strftime("%m-%d %H:%M")
    _ST5_BT_HISTORY.insert(0, entry)
    del _ST5_BT_HISTORY[40:]


@app.get("/st5/backtest_history")
def st5_backtest_history():
    return {"history": _ST5_BT_HISTORY}


@app.get("/st5/state")
def st5_state():
    return _clean(ST5.snapshot())


@app.get("/st5/pairs")
def st5_pairs():
    from .st5.service import ST5_PAIRS
    return {"pairs": [{"id": pid, "ord": s[0], "pref": s[1], "issuer": s[2], "label": s[3]}
                      for pid, s in ST5_PAIRS.items()]}


@app.post("/st5/config")
def st5_set_config(payload: dict):
    """Обновить параметры стратегии/риска st5 (блокируется при открытых позициях)."""
    _st5_guard_no_position("смена параметров")
    s = ST5.cfg.strategy
    r = ST5.cfg.risk

    def _num(key, lo, hi, cur):
        if key not in payload or payload[key] is None:
            return cur
        try:
            v = float(payload[key])
        except (TypeError, ValueError):
            raise HTTPException(400, f"{key}: не число")
        if not (lo <= v <= hi):
            raise HTTPException(400, f"{key}: вне [{lo}, {hi}]")
        return v

    s.z_entry = _num("z_entry", 0.5, 5.0, s.z_entry)
    s.z_stop = _num("z_stop", 1.0, 10.0, s.z_stop)
    s.z_take_partial = _num("z_take_partial", 0.0, 3.0, s.z_take_partial)
    s.z_exit_full = _num("z_exit_full", 0.0, 2.0, s.z_exit_full)
    s.hurst_max = _num("hurst_max", 0.3, 0.8, s.hurst_max)
    s.adf_p_enter = _num("adf_p_enter", 0.001, 0.5, s.adf_p_enter)
    s.rv_ratio_max = _num("rv_ratio_max", 0.5, 5.0, s.rv_ratio_max)
    r.max_open_positions = int(_num("max_open_positions", 1, 3, r.max_open_positions))
    r.max_go_per_trade_rub = _num("max_go_per_trade_rub", 0, 10_000_000, r.max_go_per_trade_rub)
    r.max_go_portfolio_rub = _num("max_go_portfolio_rub", 0, 50_000_000, r.max_go_portfolio_rub)
    if "quantity_lots" in payload:
        lots = int(_num("quantity_lots", 1, 100, ST5.cfg.execution.quantity_lots))
        ST5.cfg.execution.quantity_lots = lots
        # применяем объём ко ВСЕМ движкам (их base_lots) и их per-pair конфигам
        for pid, eng in ST5.engines.items():
            eng.base_lots = lots
            ST5.pair_cfgs[pid].execution.quantity_lots = lots
    if "require_dz_confirm" in payload:
        s.require_dz_confirm = bool(payload["require_dz_confirm"])
    if "trading_enabled" in payload:
        r.trading_enabled = bool(payload["trading_enabled"])
    ST5.save_session()
    return {"ok": True, "config": ST5.cfg.model_dump()}


@app.post("/st5/pair-enabled")
def st5_pair_enabled(pair: str, on: bool = True):
    """Включить/выключить торговлю пары (чекбокс). Выключенную пару live-цикл пропускает.
    Блокируется, если по паре открыта позиция (сначала закрыть)."""
    from .st5.service import ST5_PAIRS
    if pair not in ST5_PAIRS:
        raise HTTPException(400, "pair: " + " | ".join(ST5_PAIRS))
    if not on and ST5.engines[pair].position is not None:
        raise HTTPException(409, f"по паре {pair} открыта позиция — закройте её перед отключением")
    ST5.enabled_pairs[pair] = on
    ST5.log_event("info", f"{pair}: торговля {'включена' if on else 'выключена'}")
    ST5.save_session()
    return {"ok": True, "pair": pair, "enabled": on}


@app.post("/st5/control/trading")
def st5_trading(on: bool = True):
    ST5.cfg.risk.trading_enabled = on
    ST5.log_event("info", f"торговля {'включена' if on else 'выключена'}")
    return {"ok": True, "trading_enabled": on}


@app.post("/st5/telegram")
def st5_telegram(payload: dict):
    """Настройки Telegram-уведомлений + (опц.) токен бота. Наружу токен НЕ отдаём (только tg_set).

    payload: enabled, chat_id, notify_entry/exit/errors/before_open, before_open_min,
    daily_summary — любые подмножества; "token" (опц.) сохраняется в env+файл (.tg_bot_token)."""
    from .st5 import notifier as _tg
    n = ST5.cfg.notify
    if "token" in payload:                      # секрет — отдельно, в файл/env, не в config
        _tg.save_bot_token(str(payload["token"]).strip())
    for k in ("enabled", "notify_entry", "notify_exit", "notify_errors",
              "notify_before_open", "daily_summary"):
        if k in payload:
            setattr(n, k, bool(payload[k]))
    if "chat_id" in payload:
        n.chat_id = str(payload["chat_id"]).strip()
    if "before_open_min" in payload:
        n.before_open_min = max(1, min(60, int(payload["before_open_min"])))
    ST5.save_session()
    ST5.log_event("info", "Telegram-уведомления настроены")
    return {"ok": True, "notify": n.model_dump(), "tg_set": _tg.has_bot_token()}


@app.post("/st5/telegram/test")
async def st5_telegram_test():
    """Отправить тестовое сообщение (проверка токена/chat_id). Игнорирует флаг data_source."""
    ok = await ST5.notifier.send("✅ <b>trade4</b> · тестовое уведомление ST5")
    return {"ok": ok}


@app.post("/st5/control/resume")
def st5_resume():
    ST5.portfolio.resume()
    ST5.portfolio.pair_halted.clear()
    ST5.log_event("info", "HALT снят (портфель и пары)")
    return {"ok": True}


@app.post("/st5/control/start")
async def st5_start():
    """Запустить ST5 live (портфель). Режим из connector.mode; sandbox/real активны в live.

    async — чтобы asyncio.create_task видел running loop (sync-эндпоинт FastAPI крутится в
    threadpool без loop → create_task падал RuntimeError и сбрасывал live)."""
    if ST5.state["live"]:
        return {"ok": True, "already": True}
    import time as _t
    ST5.state["live"] = True
    ST5.state["live_intent"] = True    # намерение торговать → автостарт после будущих рестартов
    ST5.state["paused_by_user"] = False
    ST5.state["session_started"] = _t.time()
    ST5.state["data_source"] = "live"
    # активируем брокерский исполнитель только для sandbox/real (на paper — вирт. движок)
    want_broker = ST5.cfg.connector.mode in ("tbank_sandbox", "tbank_real")
    ST5.state["sandbox_active"] = bool(want_broker and ST5.cfg.connector.account_id)

    ST5.start_live()
    if ST5._live_task is None:
        ST5.state["live"] = False   # нет event loop (тест/CLI) — не стартуем фоном
    ST5.save_session()              # персистим live_intent сразу (автостарт переживёт рестарт)
    return {"ok": True, "mode": ST5.cfg.connector.mode, "sandbox_active": ST5.state["sandbox_active"]}


@app.post("/st5/control/stop")
def st5_stop():
    _st5_guard_no_position("пауза")
    ST5.state["live"] = False
    ST5.state["live_intent"] = False   # оператор сам остановил → НЕ автостартовать после рестарта
    ST5.state["paused_by_user"] = True
    ST5.save_session()
    return {"ok": True}


@app.post("/st5/control/flat-all")
def st5_flat_all(payload: dict):
    """Паник-закрытие ВСЕХ позиций портфеля по рынку (требует confirm). НЕ блокируется."""
    if not payload or not payload.get("confirm"):
        raise HTTPException(400, "нужно подтверждение: {\"confirm\": true}")
    closed = []
    for pid, eng in ST5.engines.items():
        if eng.position is not None:
            p = eng.position
            ord_px = p.ord_entry      # грубо: по цене входа (нет свежего бара под рукой)
            pref_px = p.pref_entry
            tr = eng._close(int(__import__("time").time() * 1000), eng.last_z or 0.0,
                            eng.last_spread, ord_px, pref_px, "flat_all")
            ST5._on_engine_trade(pid, eng, tr, ord_px, pref_px)
            closed.append(pid)
    ST5.save_session()
    return {"ok": True, "closed": closed}


@app.post("/st5/control/reconcile")
def st5_reconcile():
    """Ручная сверка движка с реальным счётом по всем парам (без ожидания нового бара).
    Для разруливания рассинхрона: на счёте позиция, движок flat → усыновляем. Только sandbox/real."""
    if not ST5.state.get("sandbox_active"):
        raise HTTPException(400, "сверка доступна только при активном брокере (sandbox/real)")
    out = []
    for pid, eng in ST5.engines.items():
        before = eng.position is not None
        ST5._reconciled.discard(pid)   # разрешить повторную сверку
        ST5._reconcile_pair(pid, eng)
        ST5._reconciled.add(pid)
        after = eng.position
        out.append({"pair": pid, "was_open": before,
                    "now": after.state.value if after else None,
                    "lots": after.lots if after else 0})
    ST5.save_session()
    return {"ok": True, "pairs": out}


@app.post("/st5/control/arm-real")
def st5_arm_real(payload: dict):
    """Взвод реальной торговли (двойной включатель, требует confirm). Сбрасывается при рестарте."""
    if not payload or not payload.get("confirm"):
        raise HTTPException(400, "нужно подтверждение: {\"confirm\": true}")
    armed = bool(payload.get("armed"))
    if armed and ST5.cfg.connector.mode != "tbank_real":
        raise HTTPException(400, "взвод доступен только в режиме tbank_real")
    ST5.arm_real(armed)
    return {"ok": True, "real_trading_armed": ST5.state["real_trading_armed"]}


_ST5_CALIB_STAGING: dict = {}   # результат автокалибровки до подтверждения (НЕ применяется авто)


@app.get("/st5/backtest")
async def st5_backtest(pair: str = "sber", days: int = 180):
    """Бэктест ST5 на истории MOEX за период (для проверки/калибровки в UI)."""
    from .st5.service import ST5_PAIRS
    if pair not in ST5_PAIRS:
        raise HTTPException(400, "pair: " + " | ".join(ST5_PAIRS))
    ao, ap = ST5_PAIRS[pair][0], ST5_PAIRS[pair][1]

    def _run():
        from .st4.config import St4Config as _C4
        from .st4 import data_feed as _feed
        from .st5.backtest import run_backtest
        c4 = _C4(); c4.instruments.asset_ordinary = ao; c4.instruments.asset_preferred = ap
        c4.strategy.candle_interval_minutes = ST5.cfg.strategy.candle_interval_minutes
        try:
            so, sp = _feed.resolve_legs(c4)
            since = _dt.now(_tz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            since = _dt.fromtimestamp(since.timestamp() - days * 86400, tz=_tz.utc)
            df = _feed.read_ohlcv_moex_range(c4, since, so.code, sp.code)
        except Exception as e:  # noqa: BLE001
            return {"error": f"данные недоступны: {e}"}
        if len(df) < 600:
            return {"error": f"мало баров: {len(df)}"}
        m = run_backtest(df, _st5_pair_cfg(pair), pair=pair, base_lots=ST5.cfg.execution.quantity_lots, fee_per_lot=2.0, half_spread_pts=0.5)
        return {"pair": pair, "legs": f"{so.code}/{sp.code}", "bars": m.bars, "trades": m.trades,
                "win_rate_pct": round(m.win_rate_pct, 0), "net_pnl_rub": round(m.net_pnl_rub, 0),
                "profit_factor": round(m.profit_factor, 2) if m.profit_factor != float("inf") else 999,
                "sharpe": round(m.sharpe, 2), "max_drawdown_pct": round(m.max_drawdown_pct, 1),
                "reasons": m.reasons}

    res = _clean(await asyncio.to_thread(_run))
    if "error" not in res:
        _st5_bt_log({"kind": "ISS", "pair": pair, "days": days, "trades": res["trades"],
                     "win_rate_pct": res["win_rate_pct"], "net_pnl_rub": res["net_pnl_rub"],
                     "sharpe": res["sharpe"], "max_drawdown_pct": res["max_drawdown_pct"]})
    return res


@app.get("/st5/daily")
async def st5_daily(pair: str = "sber", days: int = 30):
    """Доходность по дням — ТРИ кривые: идеал без издержек / идеал с издержками / реальный live.

    Разница идеал−с_издержками = стоимость исполнения; идеал−реал = упущенное (бот застал не всё).
    """
    from .st5.service import ST5_PAIRS
    if pair not in ST5_PAIRS:
        raise HTTPException(400, "pair: " + " | ".join(ST5_PAIRS))
    ao, ap = ST5_PAIRS[pair][0], ST5_PAIRS[pair][1]

    def _run():
        import datetime as _dtmod
        from collections import defaultdict
        from .st4.config import St4Config as _C4
        from .st4 import data_feed as _feed
        from .st5.engine import ST5Engine
        c4 = _C4(); c4.instruments.asset_ordinary = ao; c4.instruments.asset_preferred = ap
        try:
            so, sp = _feed.resolve_legs(c4)
            since = _dt.now(_tz.utc) - _dtmod.timedelta(days=max(days, 40) + 20)  # +прогрев
            df = _feed.read_ohlcv_moex_range(c4, since, so.code, sp.code)
        except Exception as e:  # noqa: BLE001
            return {"error": f"данные недоступны: {e}"}
        if len(df) < 600:
            return {"error": f"мало баров: {len(df)}"}

        def day_pnl(fee, hs):
            eng = ST5Engine(pair, _st5_pair_cfg(pair), base_lots=ST5.cfg.execution.quantity_lots, fee_per_lot=fee, half_spread_pts=hs)
            for ts, row in df.iterrows():
                eng.step(int(ts), float(row["price_a"]), float(row["price_b"]))
            buckets = defaultdict(float)
            for t in eng.trades:
                d = _dtmod.datetime.fromtimestamp(t.exit_ts / 1000, _MSK).strftime("%Y-%m-%d")
                buckets[d] += t.net_pnl_rub
            return buckets

        ideal = day_pnl(0.0, 0.0)            # без издержек
        with_costs = day_pnl(2.0, 0.5)       # с комиссиями + half-spread
        # реальный live: фактические сделки бота по этой паре
        real = defaultdict(float)
        for t in ST5.trades:
            if t.get("pair") == pair and t.get("exit_ts"):
                d = _dtmod.datetime.fromtimestamp(t["exit_ts"] / 1000, _MSK).strftime("%Y-%m-%d")
                real[d] += t.get("net_pnl_rub", 0)
        # последние N дней с любой активностью
        all_days = sorted(set(ideal) | set(with_costs) | set(real))[-days:]
        rows = [{"date": d, "ideal": round(ideal.get(d, 0)), "with_costs": round(with_costs.get(d, 0)),
                 "real": round(real.get(d, 0))} for d in all_days]
        # ИТОГИ — только по дням, где бот РЕАЛЬНО торговал (real!=0). Иначе total_ideal суммирует
        # бэктест за дни, когда бота ещё не существовало (real=0) → missed раздувается фикцией
        # «упущенного», хотя бот просто не работал в эти дни. Сравнивать ideal/real можно лишь
        # на сопоставимом отрезке. rows остаются полными (весь бэктест — для контекста).
        active = {d for d, v in real.items() if v}
        sum_ideal = sum(ideal.get(d, 0) for d in active)
        sum_costs = sum(with_costs.get(d, 0) for d in active)
        sum_real = sum(real.get(d, 0) for d in active)
        return {"pair": pair, "legs": f"{so.code}/{sp.code}", "rows": rows,
                "active_days": len(active),    # дней, по которым считаются итоги (real!=0)
                "total_ideal": round(sum_ideal),
                "total_with_costs": round(sum_costs),
                "total_real": round(sum_real),
                "cost_of_execution": round(sum_ideal - sum_costs),
                "missed": round(sum_costs - sum_real)}

    return _clean(await asyncio.to_thread(_run))


@app.get("/st5/margin_timeline")
async def st5_margin_timeline(date: str = ""):
    """Залог (ГО) по 10-минутным слотам за день: сколько обеспечения требовалось в течение дня
    и сколько позиций стояло в каждый момент. Реконструкция из журнала сделок (entry_ts…exit_ts)
    + текущие открытые позиции движков (ещё не в журнале). ГО = Σ leg_margin×лоты ноги×go_factor
    (β-ноги: лоты обычки ≠ лотам префа; legacy-записи без ord_lots → равные ноги)."""
    from .st5.service import St5Portfolio
    SLOT = 10 * 60 * 1000    # 10 минут в мс

    def _run():
        gf = ST5.portfolio.go_factor or 1.0
        # день в МСК (по умолчанию — сегодня)
        day = date or _dt.now(_MSK).strftime("%Y-%m-%d")
        d0 = _dt.strptime(day, "%Y-%m-%d").replace(tzinfo=_MSK)
        day_start = int(d0.timestamp() * 1000)
        day_end = day_start + 24 * 3600 * 1000
        # интервалы позиций за этот день: [(entry_ms, exit_ms|None, pair, pref_lots, ord_lots), …]
        intervals = []
        for t in ST5.trades:
            e, x = t.get("entry_ts"), t.get("exit_ts")
            if not e or not x:
                continue
            if x <= day_start or e >= day_end:      # не пересекает день
                continue
            lots = t.get("lots", 1)
            intervals.append((e, x, t.get("pair"), lots, t.get("ord_lots") or lots))
        # текущие открытые позиции движков (ещё не закрыты → нет в trades), exit=None (по «сейчас»)
        now_ms = int(_dt.now(_MSK).timestamp() * 1000)
        for pid, eng in ST5.engines.items():
            p = getattr(eng, "position", None)
            if p is not None and getattr(p, "entry_ts", 0) and p.entry_ts < day_end:
                intervals.append((p.entry_ts, None, pid, p.lots,
                                  getattr(p, "ord_lots", 0) or p.lots))
        if not intervals:
            return {"date": day, "rows": [], "peak_rub": 0, "peak_positions": 0}
        # границы сетки слотов — от первого входа до последнего выхода (в пределах дня)
        lo = max(day_start, min(iv[0] for iv in intervals))
        hi = min(day_end, max((iv[1] or now_ms) for iv in intervals))
        lo -= lo % SLOT                             # выровнять к сетке 10м
        rows = []
        peak_rub = 0.0
        peak_pos = 0
        t = lo
        while t < hi:
            s0, s1 = t, t + SLOT
            go = 0.0
            cnt = 0
            open_pairs = []
            for e, x, pair, pref_lots, ord_lots in intervals:
                xe = x if x is not None else now_ms
                if e < s1 and xe > s0:              # позиция активна в слоте
                    m_ord, m_pref = St5Portfolio.pair_leg_margins(pair)
                    go += (m_ord * ord_lots + m_pref * pref_lots) * gf
                    cnt += 1
                    open_pairs.append(pair)
            if cnt:                                  # показываем только слоты с позициями
                hhmm = _dt.fromtimestamp(s0 / 1000, _MSK).strftime("%H:%M")
                rows.append({"time": hhmm, "ts": s0, "go_rub": round(go),
                             "positions": cnt, "pairs": open_pairs})
                peak_rub = max(peak_rub, go)
                peak_pos = max(peak_pos, cnt)
            t += SLOT
        return {"date": day, "go_factor": round(gf, 3), "rows": rows,
                "peak_rub": round(peak_rub), "peak_positions": peak_pos,
                "slot_min": 10}

    return _clean(await asyncio.to_thread(_run))


@app.get("/st5/backtest_tbank")
async def st5_backtest_tbank(pair: str = "sber"):
    """Бэктест ST5 на РЕАЛЬНЫХ котировках T-Bank (тот же источник, что sandbox-ордера; ~неделя)."""
    from .st5.service import ST5_PAIRS
    if pair not in ST5_PAIRS:
        raise HTTPException(400, "pair: " + " | ".join(ST5_PAIRS))
    ao, ap = ST5_PAIRS[pair][0], ST5_PAIRS[pair][1]

    def _run():
        from .st4.config import St4Config as _C4
        from .st4 import data_feed as _feed
        from .st4 import tbank_sandbox as _sb
        from .st5.backtest import run_backtest
        c4 = _C4(); c4.instruments.asset_ordinary = ao; c4.instruments.asset_preferred = ap
        try:
            so, sp = _feed.resolve_legs(c4)
            uid_o = _sb.find_future(so.code)["uid"]; uid_p = _sb.find_future(sp.code)["uid"]
            df = _feed.read_ohlcv_tbank(c4, 1000, uid_o, uid_p)
        except Exception as e:  # noqa: BLE001
            return {"error": f"T-Bank данные недоступны: {e}"}
        if len(df) < 200:
            return {"error": f"мало баров T-Bank: {len(df)} (нужно прогреть фильтры)"}
        m = run_backtest(df, _st5_pair_cfg(pair), pair=pair, base_lots=ST5.cfg.execution.quantity_lots, fee_per_lot=2.0, half_spread_pts=0.5)
        return {"pair": pair, "legs": f"{so.code}/{sp.code}", "source": "T-Bank", "bars": m.bars,
                "trades": m.trades, "win_rate_pct": round(m.win_rate_pct, 0),
                "net_pnl_rub": round(m.net_pnl_rub, 0),
                "profit_factor": round(m.profit_factor, 2) if m.profit_factor != float("inf") else 999,
                "sharpe": round(m.sharpe, 2), "max_drawdown_pct": round(m.max_drawdown_pct, 1),
                "reasons": m.reasons}

    res = _clean(await asyncio.to_thread(_run))
    if "error" not in res:
        _st5_bt_log({"kind": "T-Bank", "pair": pair, "days": None, "trades": res["trades"],
                     "win_rate_pct": res["win_rate_pct"], "net_pnl_rub": res["net_pnl_rub"],
                     "sharpe": res["sharpe"], "max_drawdown_pct": res["max_drawdown_pct"]})
    return res


@app.get("/st5/weekly_by_pair")
async def st5_weekly_by_pair():
    """Доходность по инструментам по РЕАЛЬНЫМ зафиксированным сделкам из журнала ST5.

    Отдельный блок на инструмент: по дням net-прибыль и комиссия фактических сделок бота
    (ST5.trades — закрытые/частично-зафиксированные), НЕ бэктест.
    """
    import datetime as _dtmod
    from collections import defaultdict
    from .st5.service import ST5_PAIRS

    # группируем реальные сделки журнала по паре и дню (по exit_ts — момент фиксации)
    by_pair = {pid: {"net": defaultdict(float), "fee": defaultdict(float), "cnt": defaultdict(int)}
               for pid in ST5_PAIRS}
    for t in ST5.trades:
        pid = t.get("pair")
        if pid not in by_pair or not t.get("exit_ts"):
            continue
        d = _dtmod.datetime.fromtimestamp(t["exit_ts"] / 1000, _MSK).strftime("%Y-%m-%d")
        by_pair[pid]["net"][d] += t.get("net_pnl_rub", 0) or 0
        by_pair[pid]["fee"][d] += t.get("fees_rub", 0) or 0   # старые сделки: fees_rub=None → 0
        by_pair[pid]["cnt"][d] += 1

    pairs = []
    for pid, spec in ST5_PAIRS.items():
        b = by_pair[pid]
        days = sorted(set(b["net"]) | set(b["cnt"]))[-7:]   # последние 7 дней с активностью
        rows = [{"date": d, "net": round(b["net"].get(d, 0)), "fee": round(b["fee"].get(d, 0)),
                 "trades": b["cnt"].get(d, 0)} for d in days]
        pairs.append({"pair": pid, "label": spec[3], "rows": rows,
                      "total_net": round(sum(b["net"].values())),
                      "total_fee": round(sum(b["fee"].values())),
                      "total_trades": sum(b["cnt"].values())})
    return _clean({"pairs": pairs, "source": "журнал live-сделок"})


@app.get("/st5/margin")
async def st5_margin():
    """Гарантийное обеспечение (ГО) всех пар + САМОПРОВЕРКА: хватает ли капитала на портфель."""
    from .st5.service import ST5_PAIRS

    def _run():
        from .st4.config import St4Config as _C4
        from .st4 import data_feed as _feed
        rows = []
        total_go_3pos = 0.0
        for pid, spec in ST5_PAIRS.items():
            c4 = _C4(); c4.instruments.asset_ordinary = spec[0]; c4.instruments.asset_preferred = spec[1]
            try:
                so, sp = _feed.resolve_legs(c4)
                m_ord = _feed.leg_margin(so.code)
                m_pref = _feed.leg_margin(sp.code)
            except Exception as e:  # noqa: BLE001
                rows.append({"pair": pid, "label": spec[3], "error": str(e)[:50]})
                continue
            lots = ST5.cfg.execution.quantity_lots
            go_pair = (m_ord + m_pref) * lots   # ГО на пару при текущем объёме (обе ноги)
            total_go_3pos += go_pair
            rows.append({"pair": pid, "label": spec[3], "legs": f"{so.code}/{sp.code}",
                         "go_ord": round(m_ord), "go_pref": round(m_pref),
                         "go_pair_10lots": round(go_pair)})   # ключ оставлен для совместимости UI
        cap = ST5.portfolio.capital_rub
        # РЕАЛЬНОЕ заблокированное ГО с биржевой хедж-скидкой — только при открытых позициях
        real_blocked = None
        if ST5.state.get("sandbox_active") and ST5.cfg.connector.account_id:
            try:
                from .st4 import tbank_sandbox as _sb
                rb = _sb.blocked_margin(ST5.cfg.connector.account_id)
                real_blocked = round(rb) if rb > 0 else None
            except Exception:  # noqa: BLE001
                real_blocked = None
        # самопроверка: ГО 3 позиций vs капитал + лимит портфеля 5%
        return {"rows": rows, "capital_rub": round(cap), "lots": ST5.cfg.execution.quantity_lots,
                "go_all_3pairs_10lots": round(total_go_3pos),
                "go_per_1lot": round(total_go_3pos / max(1, ST5.cfg.execution.quantity_lots)),
                "go_pct_of_capital": round(total_go_3pos / cap * 100, 1) if cap > 0 else 0,
                "real_blocked_rub": real_blocked,   # фактически заблокировано на счёте (None если flat)
                "portfolio_limit_pct": ST5.cfg.risk.risk_per_portfolio_pct * 100,
                "self_check_ok": total_go_3pos < cap * 0.5}   # ГО 3 поз. должно быть < 50% капитала

    return _clean(await asyncio.to_thread(_run))


@app.get("/st5/orderbook")
async def st5_orderbook(pair: str = "sber", depth: int = 10):
    """Биржевой стакан (DOM) обеих ног пары: bids/asks с объёмами. Только при активном брокере."""
    from .st5.service import ST5_PAIRS
    if pair not in ST5_PAIRS:
        raise HTTPException(400, "pair: " + " | ".join(ST5_PAIRS))
    if not ST5.state.get("sandbox_active"):
        raise HTTPException(400, "стакан доступен только при активном брокере (sandbox/real)")

    def _run():
        from .st4.config import St4Config as _C4
        from .st4 import data_feed as _feed
        from .st4 import tbank_sandbox as _sb
        spec = ST5_PAIRS[pair]
        c4 = _C4(); c4.instruments.asset_ordinary = spec[0]; c4.instruments.asset_preferred = spec[1]
        c4.strategy.candle_interval_minutes = ST5.cfg.strategy.candle_interval_minutes
        try:
            so, sp = _feed.resolve_legs(c4)
            uo = _sb.find_future(so.code)["uid"]
            up = _sb.find_future(sp.code)["uid"]
            return {"pair": pair, "depth": depth,
                    "ord": {"code": so.code, **_sb.order_book(uo, depth)},
                    "pref": {"code": sp.code, **_sb.order_book(up, depth)}}
        except Exception as e:  # noqa: BLE001
            return {"error": f"стакан недоступен: {e}"}

    return _clean(await asyncio.to_thread(_run))


_ST5_SWEEP_PARAMS = {
    "z_entry": [1.75, 2.0, 2.25, 2.5, 2.75, 3.0],
    "z_stop": [3.5, 4.0, 4.25, 4.5, 5.0, 6.0],
    "z_take_partial": [0.5, 0.75, 1.0, 1.25, 1.5],
    "z_exit_full": [0.1, 0.25, 0.35, 0.5, 0.75],
    "hurst_max": [0.45, 0.50, 0.55, 0.60, 0.65, 0.70],
    "adf_p_enter": [0.01, 0.025, 0.05, 0.10, 0.15],
    "rv_ratio_max": [1.2, 1.5, 1.7, 2.0, 2.5, 3.0],
    "kalman_delta": [1e-5, 5e-5, 1e-4, 5e-4, 1e-3],
    "z_ema_span": [100, 150, 200, 250],
    "z_std_window": [100, 150, 200, 250],
}


@app.get("/st5/sweep")
async def st5_sweep(pair: str = "sber", param: str = "z_entry", days: int = 365):
    """Расширенная аналитика: перебор ОДНОГО параметра по сетке на большом объёме, полные
    метрики (Sharpe/Sortino/Calmar/PF/maxDD/expectancy/avg hold). param из _ST5_SWEEP_PARAMS."""
    from .st5.service import ST5_PAIRS
    if pair not in ST5_PAIRS:
        raise HTTPException(400, "pair: " + " | ".join(ST5_PAIRS))
    if param not in _ST5_SWEEP_PARAMS:
        raise HTTPException(400, "param: " + " | ".join(_ST5_SWEEP_PARAMS))
    ao, ap = ST5_PAIRS[pair][0], ST5_PAIRS[pair][1]

    def _run():
        from .st4.config import St4Config as _C4
        from .st4 import data_feed as _feed
        from .st5.backtest import run_backtest
        from .st5.config import St5Config as _C5
        c4 = _C4(); c4.instruments.asset_ordinary = ao; c4.instruments.asset_preferred = ap
        try:
            so, sp = _feed.resolve_legs(c4)
            since = _dt.now(_tz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            since = _dt.fromtimestamp(since.timestamp() - days * 86400, tz=_tz.utc)
            df = _feed.read_ohlcv_moex_range(c4, since, so.code, sp.code)
        except Exception as e:  # noqa: BLE001
            return {"error": f"данные недоступны: {e}"}
        if len(df) < 600:
            return {"error": f"мало баров: {len(df)}"}
        rows = []
        base = _st5_pair_cfg(pair)   # стартуем с per-pair настроек пары, перебираем один параметр
        for val in _ST5_SWEEP_PARAMS[param]:
            c = _C5(**base.model_dump())
            setattr(c.strategy, param, val)
            m = run_backtest(df, c, pair=pair, base_lots=ST5.cfg.execution.quantity_lots, fee_per_lot=2.0, half_spread_pts=0.5)
            rows.append({"value": val, "trades": m.trades, "win_rate_pct": round(m.win_rate_pct, 0),
                         "net_pnl_rub": round(m.net_pnl_rub, 0),
                         "sharpe": round(m.sharpe, 2), "sortino": round(m.sortino, 2),
                         "calmar": round(m.calmar, 1),
                         "profit_factor": round(m.profit_factor, 2) if m.profit_factor != float("inf") else 999,
                         "max_drawdown_pct": round(m.max_drawdown_pct, 1),
                         "expectancy": round(m.expectancy, 0), "avg_bars_held": round(m.avg_bars_held, 1)})
        valid = [r for r in rows if r["trades"] >= 3]
        best = max(valid, key=lambda r: r["sharpe"]) if valid else None
        return {"pair": pair, "param": param, "legs": f"{so.code}/{sp.code}", "bars": len(df),
                "current": getattr(base.strategy, param), "rows": rows, "best": best}

    res = _clean(await asyncio.to_thread(_run))
    if "error" not in res and res.get("best"):
        b = res["best"]
        _st5_bt_log({"kind": f"sweep:{param}", "pair": pair, "days": days, "trades": b["trades"],
                     "win_rate_pct": b["win_rate_pct"], "net_pnl_rub": b["net_pnl_rub"],
                     "sharpe": b["sharpe"], "max_drawdown_pct": b["max_drawdown_pct"]})
    return res


@app.get("/st5/grid")
async def st5_grid(pair: str = "sber", days: int = 180):
    """Грид-тест влияющих параметров (z_entry × z_stop × hurst_max) — показать выигрышную комбу."""
    from .st5.service import ST5_PAIRS
    if pair not in ST5_PAIRS:
        raise HTTPException(400, "pair: " + " | ".join(ST5_PAIRS))
    ao, ap = ST5_PAIRS[pair][0], ST5_PAIRS[pair][1]

    def _run():
        from .st4.config import St4Config as _C4
        from .st4 import data_feed as _feed
        from .st5.backtest import run_backtest
        from .st5.config import St5Config as _C5
        c4 = _C4(); c4.instruments.asset_ordinary = ao; c4.instruments.asset_preferred = ap
        try:
            so, sp = _feed.resolve_legs(c4)
            since = _dt.now(_tz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            since = _dt.fromtimestamp(since.timestamp() - days * 86400, tz=_tz.utc)
            df = _feed.read_ohlcv_moex_range(c4, since, so.code, sp.code)
        except Exception as e:  # noqa: BLE001
            return {"error": f"данные недоступны: {e}"}
        if len(df) < 600:
            return {"error": f"мало баров: {len(df)}"}
        rows = []
        base = _st5_pair_cfg(pair)   # per-pair база, поверх неё перебираем z_entry×z_stop×hurst_max
        for z_entry in (2.0, 2.25, 2.5):
            for z_stop in (4.0, 4.25, 5.0):
                for hmax in (0.55, 0.60):
                    c = _C5(**base.model_dump())
                    c.strategy.z_entry = z_entry; c.strategy.z_stop = z_stop; c.strategy.hurst_max = hmax
                    m = run_backtest(df, c, pair=pair, base_lots=ST5.cfg.execution.quantity_lots, fee_per_lot=2.0, half_spread_pts=0.5)
                    rows.append({"z_entry": z_entry, "z_stop": z_stop, "hurst_max": hmax,
                                 "trades": m.trades, "win_rate_pct": round(m.win_rate_pct, 0),
                                 "net_pnl_rub": round(m.net_pnl_rub, 0),
                                 "sharpe": round(m.sharpe, 2),
                                 "profit_factor": round(m.profit_factor, 2) if m.profit_factor != float("inf") else 999,
                                 "max_drawdown_pct": round(m.max_drawdown_pct, 1)})
        # выигрышная комбинация — по Sharpe среди тех, где ≥3 сделок
        valid = [r for r in rows if r["trades"] >= 3]
        best = max(valid, key=lambda r: r["sharpe"]) if valid else None
        rows.sort(key=lambda r: r["sharpe"], reverse=True)
        return {"pair": pair, "legs": f"{so.code}/{sp.code}", "bars": len(df), "rows": rows, "best": best}

    return _clean(await asyncio.to_thread(_run))


@app.post("/st5/calibrate")
async def st5_calibrate(pair: str = "sber", days: int = 180):
    """Walk-forward автокалибровка → STAGING (НЕ применяется авто, нужно подтверждение)."""
    from .st5.service import ST5_PAIRS
    if pair not in ST5_PAIRS:
        raise HTTPException(400, "pair: " + " | ".join(ST5_PAIRS))
    ao, ap = ST5_PAIRS[pair][0], ST5_PAIRS[pair][1]

    def _run():
        from .st4.config import St4Config as _C4
        from .st4 import data_feed as _feed
        from .st5.calibrate import walk_forward
        c4 = _C4(); c4.instruments.asset_ordinary = ao; c4.instruments.asset_preferred = ap
        try:
            so, sp = _feed.resolve_legs(c4)
            since = _dt.now(_tz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            since = _dt.fromtimestamp(since.timestamp() - days * 86400, tz=_tz.utc)
            df = _feed.read_ohlcv_moex_range(c4, since, so.code, sp.code)
        except Exception as e:  # noqa: BLE001
            return {"error": f"данные недоступны: {e}"}
        return walk_forward(df, ST5.cfg, pair=pair)

    res = await asyncio.to_thread(_run)
    if "error" not in res:
        _ST5_CALIB_STAGING[pair] = res   # в staging — применение отдельным эндпоинтом
    return _clean(res)


@app.post("/st5/calibrate/apply")
def st5_calibrate_apply(pair: str = "sber", confirm: bool = False):
    """Применить параметры из staging (требует confirm и отсутствия открытых позиций)."""
    if not confirm:
        raise HTTPException(400, "нужно подтверждение: confirm=true")
    _st5_guard_no_position("применение калибровки")
    if pair not in _ST5_CALIB_STAGING:
        raise HTTPException(404, "нет staging-калибровки для пары")
    params = _ST5_CALIB_STAGING[pair]["best_params"]
    for k, v in params.items():
        setattr(ST5.cfg.strategy, k, v)
    ST5.log_event("info", f"калибровка применена ({pair}): {params}")
    ST5.save_session()
    return {"ok": True, "applied": params}


# ---------- версионирование стратегии (сохранение/откат per-pair параметров) ----------

def _st5_backtest_overrides(overrides: dict, days: int = 90) -> dict:
    """Бэктест per-pair параметров (overrides {pid: {param}}) на ISS за days дней + split-half.
    Возвращает {pid: {net,win,sharpe,trades, h1_net,h2_net}} — метрики для приложения к стратегии."""
    from .st5.service import ST5_PAIRS, St5Session
    from .st4.config import St4Config as _C4
    from .st4 import data_feed as _feed
    from .st5.backtest import run_backtest

    def _pcfg(pid):
        # конфиг пары с НАЛОЖЕННЫМ оверрайдом (как сделает apply_overrides), не трогая живой ST5
        tmp = St5Session()
        if overrides.get(pid):
            tmp.pair_overrides[pid] = {k: v for k, v in overrides[pid].items()
                                       if k in St5Session.OVERRIDE_KEYS}
        return tmp._pair_cfg(pid)

    out: dict = {}
    for pid in ST5_PAIRS:
        ao, ap = ST5_PAIRS[pid][0], ST5_PAIRS[pid][1]
        c4 = _C4(); c4.instruments.asset_ordinary = ao; c4.instruments.asset_preferred = ap
        c4.strategy.candle_interval_minutes = ST5.cfg.strategy.candle_interval_minutes
        try:
            so, sp = _feed.resolve_legs(c4)
            since = _dt.fromtimestamp(_dt.now(_tz.utc).timestamp() - days * 86400, tz=_tz.utc)
            df = _feed.read_ohlcv_moex_range(c4, since, so.code, sp.code)
        except Exception as e:  # noqa: BLE001
            out[pid] = {"error": f"данные недоступны: {e}"}
            continue
        if len(df) < 600:
            out[pid] = {"error": f"мало баров: {len(df)}"}
            continue
        cfg = _pcfg(pid)
        lots = ST5.cfg.execution.quantity_lots
        m = run_backtest(df, cfg, pair=pid, base_lots=lots, fee_per_lot=2.0, half_spread_pts=0.5)
        # split-half: устойчивость (выигрыш не в одной половине, а в обеих)
        mid = len(df) // 2
        h1 = run_backtest(df.iloc[:mid], cfg, pair=pid, base_lots=lots, fee_per_lot=2.0, half_spread_pts=0.5)
        h2 = run_backtest(df.iloc[mid:], cfg, pair=pid, base_lots=lots, fee_per_lot=2.0, half_spread_pts=0.5)
        out[pid] = {
            "net": round(m.net_pnl_rub, 0), "win": round(m.win_rate_pct, 0),
            "sharpe": round(m.sharpe, 2), "trades": m.trades, "bars": m.bars,
            "h1_net": round(h1.net_pnl_rub, 0), "h2_net": round(h2.net_pnl_rub, 0),
            "stable": bool(h1.net_pnl_rub > 0 and h2.net_pnl_rub > 0),
        }
    return out


@app.get("/st5/strategies")
def st5_strategies_list():
    """Список сохранённых версий стратегии (новые сверху) + текущие действующие параметры."""
    from .st5 import strategy_store as store
    return _clean({"current": ST5.capture_current(),
                   "overrides": ST5.pair_overrides,
                   "strategies": store.list_strategies()})


@app.post("/st5/strategies/save")
async def st5_strategies_save(payload: dict):
    """Снять ТЕКУЩИЕ действующие параметры + посчитать бэктест (90д ISS + split-half) → файл.
    payload: {name, note, params?(опц. — иначе берём действующие)}."""
    from .st5 import strategy_store as store
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "нужно имя стратегии (name)")
    params = payload.get("params") or ST5.capture_current()
    bt = await asyncio.to_thread(_st5_backtest_overrides, params, 90)
    sid = store.save_strategy(name=name, params=params, backtest=_clean(bt),
                              window="90д ISS (split-half)", note=(payload.get("note") or ""),
                              source=payload.get("source") or "manual")
    ST5.log_event("info", f"стратегия сохранена: {name} ({sid})")
    return _clean({"ok": True, "id": sid, "backtest": bt})


@app.post("/st5/strategies/apply")
def st5_strategies_apply(payload: dict):
    """Применить сохранённую стратегию (откат/смена) к живым движкам. Требует confirm и flat.
    payload: {id, confirm}."""
    from .st5 import strategy_store as store
    if not payload.get("confirm"):
        raise HTTPException(400, "нужно подтверждение: confirm=true")
    rec = store.load_strategy(payload.get("id") or "")
    if rec is None:
        raise HTTPException(404, "стратегия не найдена")
    ok, reason = ST5.apply_overrides(rec.get("params") or {})
    if not ok:
        raise HTTPException(409, reason)
    ST5.log_event("warn", f"применена стратегия «{rec['name']}» ({rec['id']})")
    return {"ok": True, "applied": rec["id"], "name": rec["name"], "params": ST5.capture_current()}


@app.post("/st5/strategies/backtest")
async def st5_strategies_backtest(payload: dict | None = None):
    """Прогнать бэктест действующих (или переданных) параметров без сохранения — превью для UI."""
    params = (payload or {}).get("params") or ST5.capture_current()
    bt = await asyncio.to_thread(_st5_backtest_overrides, params, (payload or {}).get("days", 90))
    return _clean({"params": params, "backtest": bt})


@app.post("/st5/connector")
def st5_connector(payload: dict):
    """Режим исполнителя st5: paper | tbank_sandbox | tbank_real (+account_id для real)."""
    _st5_guard_no_position("смена коннектора")
    mode = payload.get("mode")
    if mode not in ("paper", "tbank_sandbox", "tbank_real"):
        raise HTTPException(400, "mode: paper | tbank_sandbox | tbank_real")
    from .st4 import tbank_sandbox as _sb
    if payload.get("token"):
        _sb.save_token(str(payload["token"]).strip())
    ST5.cfg.connector.mode = mode
    if "account_id" in payload:
        ST5.cfg.connector.account_id = str(payload["account_id"]).strip()
    if mode == "tbank_real" and not ST5.cfg.connector.account_id:
        raise HTTPException(400, "для tbank_real обязателен account_id")
    ST5.state["real_trading_armed"] = False   # смена режима снимает взвод
    ST5.save_session()
    return {"ok": True, "connector_mode": mode, "token_set": _sb.has_token()}


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
    _guard_no_position(ST4, "смена параметров")
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
    if "chart_interval_min" in payload:
        ci = int(payload["chart_interval_min"])
        if ci not in (0, 1, 5, 10):
            raise HTTPException(400, "chart_interval_min: 0 (=торговый) | 1 | 5 | 10")
        # график детальнее торговли: только меньший ТФ и только через T-Bank (1m/5m real-time)
        if ci and ci >= s.candle_interval_minutes:
            raise HTTPException(400, "chart_interval_min должен быть < торгового ТФ")
        s.chart_interval_minutes = ci
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
    _guard_no_position(ST4, "пауза")
    ST4.state["live"] = False
    ST4.state["paused_by_user"] = True   # намеренная остановка — автостарт не возобновляет
    ST4.save_session()
    return {"ok": True}


@app.post("/st4/player/start")
async def st4_player_start(limit: int = 1500, pair: str = "sber"):
    """Запустить/возобновить синтетический плеер (офлайн-демо FSM)."""
    ST4 = _st4(pair)
    _guard_no_position(ST4, "запуск плеера")
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
    _guard_no_position(ST4, "сброс")
    ST4.reset_engine(real=(ST4.state["data_source"] == "live"))
    return {"ok": True}


@app.post("/st4/reload-params")
async def st4_reload_params(payload: dict | None = None, pair: str = "sber"):
    """Горячо переприменить параметры стратегии ОДНОЙ пары — без рестарта сервиса и без
    потери открытой позиции. Другие пары не затрагиваются. История/BB прогреются заново."""
    ST4 = _st4(pair)
    _guard_no_position(ST4, "переприменение параметров")
    res = await asyncio.to_thread(ST4.apply_pair_params, payload or None)
    return {"ok": True, "pair": pair, **res}


@app.post("/st4/restore-position")
def st4_restore_position(payload: dict, pair: str = "sber"):
    """Восстановить открытую позицию в ЖИВОМ движке (без файловой гонки)."""
    from .st4.models import BotState, LegPosition, Position, Role
    ST4 = _st4(pair)
    _guard_no_position(ST4, "восстановление позиции")   # уже есть позиция — не перезатираем
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
    """Установить режим исполнителя (paper|tbank_sandbox|tbank_real) и (опц.) API-токен T-Bank.

    Токен сохраняется в файл .tbank_token (0600, в .gitignore) и в env процесса. В ответе
    НЕ возвращается. Sandbox/real активны только в live; при недоступности reset откатывает в paper.

    ⚠️ tbank_real (боевой) требует ОБЯЗАТЕЛЬНЫЙ account_id, проверяемый против реальных счетов.
    Установка режима ордера НЕ шлёт — нужен ещё взвод через /st4/control/arm-real."""
    from .st4 import tbank_sandbox as _sb

    ST4 = _st4(pair)
    _guard_no_position(ST4, "смена коннектора")
    mode = payload.get("mode")
    if mode not in ("paper", "tbank_sandbox", "tbank_real"):
        raise HTTPException(400, "mode: paper | tbank_sandbox | tbank_real")
    token = (payload.get("token") or "").strip()
    if token:
        _sb.save_token(token)
    if mode in ("tbank_sandbox", "tbank_real") and not _sb.has_token():
        raise HTTPException(400, "нужен токен T-Bank (вставьте в поле API-токен)")
    if mode == "tbank_real":
        from .st4 import tbank_live as _live
        account_id = str(payload.get("account_id") or ST4.cfg.connector.account_id or "").strip()
        if not account_id:
            raise HTTPException(400, "режим tbank_real требует явный account_id реального счёта")
        try:
            if not _live.account_is_open(account_id):
                raise HTTPException(400, f"счёт {account_id} не найден или закрыт")
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"не удалось проверить счёт через T-Bank: {e}")
        ST4.cfg.connector.account_id = account_id
    if "payin_rub" in payload:
        try:
            ST4.cfg.connector.payin_rub = int(payload["payin_rub"])
        except (TypeError, ValueError):
            raise HTTPException(400, "payin_rub: не число")
    ST4.cfg.connector.mode = mode
    ST4.state["real_trading_armed"] = False    # смена режима снимает взвод (safe)
    ST4.state["live"] = ST4.state["player"] = False
    ST4.reset_engine(real=(ST4.state["data_source"] == "live"))
    return {"ok": True, "connector_mode": ST4.cfg.connector.mode,
            "token_set": _sb.has_token(),
            "fell_back": ST4.cfg.connector.mode != mode}


@app.post("/st4/control/arm-real")
def st4_arm_real(payload: dict, pair: str = "sber"):
    """⚠️ Двойной включатель реальной торговли. armed=true ВЗВОДИТ отправку боевых ордеров
    (нужен confirm=true). Действует только в режиме tbank_real. Сбрасывается при рестарте.
    Без взвода режим tbank_real ордера НЕ шлёт (только читает баланс/ведёт paper-логику)."""
    ST4 = _st4(pair)
    armed = bool(payload.get("armed"))
    if armed:
        if not payload.get("confirm"):
            raise HTTPException(400, "взвод реальной торговли требует confirm=true")
        if ST4.cfg.connector.mode != "tbank_real":
            raise HTTPException(400, "взвод доступен только в режиме tbank_real")
    ST4.state["real_trading_armed"] = armed
    ST4.log_event("warn" if armed else "info",
                  "🔴 РЕАЛЬНАЯ ТОРГОВЛЯ ВЗВЕДЕНА" if armed else "реальная торговля снята со взвода")
    ST4.save_session()
    return {"ok": True, "real_trading_armed": armed}


@app.get("/st4/broker-balance")
async def st4_broker_balance(pair: str = "sber"):
    """Реальный баланс/позиции с боевого счёта (read-only, OperationsService.GetPortfolio).
    Доступен при наличии токена и заданного account_id — независимо от текущего режима."""
    from .st4 import tbank_live as _live
    ST4 = _st4(pair)
    account_id = ST4.cfg.connector.account_id
    if not account_id:
        return {"ok": False, "error": "account_id не задан (переключите в tbank_real с реальным счётом)"}
    try:
        pf = await asyncio.to_thread(_live.portfolio, account_id)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
    poss = []
    for p in pf.get("positions", []):
        poss.append({
            "type": p.get("instrumentType"),
            "uid": p.get("instrumentUid", ""),
            "qty": _live._q_to_float(p.get("quantity")),
            "avg": _live._q_to_float(p.get("averagePositionPrice")),
        })
    return {
        "ok": True,
        "account_id": account_id,
        "total_rub": _live._q_to_float(pf.get("totalAmountPortfolio")),
        "futures_rub": _live._q_to_float(pf.get("totalAmountFutures")),
        "money_rub": _live._q_to_float(pf.get("totalAmountCurrencies")),
        "expected_yield": _live._q_to_float(pf.get("expectedYield")),
        "positions": poss,
        "armed": bool(ST4.state.get("real_trading_armed")),
        "mode": ST4.cfg.connector.mode,
    }


@app.post("/st4/connector/forget-token")
def st4_forget_token():
    """Удалить сохранённый токен (из файла и env). Откатить в paper ВСЕ пары (токен общий)."""
    # токен общий → reset_engine для всех пар. Блокируем, если открыта позиция У ЛЮБОЙ пары.
    busy = [pid for pid, s4 in ST4S.items() if s4.engine.position is not None]
    if busy:
        raise HTTPException(409, f"активная позиция ({', '.join(busy)}) — забыть токен невозможно. "
                                 "Сначала закройте позиции (flat-all).")
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
