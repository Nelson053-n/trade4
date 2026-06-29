"""Тесты слоя авторизации/CSRF/rate-limit (аудит безопасности H1/H2/H3/C2).

AUTH_ENABLED/_ALLOW_NOAUTH/секрет вычисляются при ИМПОРТЕ app.api — поэтому каждый режим
проверяем через importlib.reload с подменённым окружением. После каждого теста модуль
перезагружаем обратно в дефолт (NOAUTH=1 из conftest), чтобы не влиять на другие тесты.
"""
from __future__ import annotations

import importlib
import os

import pytest
from fastapi.testclient import TestClient


def _reload_api(env: dict):
    """Перезагрузить app.api с заданным окружением. Возвращает модуль."""
    old = {k: os.environ.get(k) for k in
           ("TRADE4_USER", "TRADE4_PASS", "TRADE4_SECRET", "TRADE4_ALLOW_NOAUTH")}
    for k in old:
        os.environ.pop(k, None)
    os.environ.update(env)
    try:
        import app.api as api
        return importlib.reload(api)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture(autouse=True)
def _restore_api():
    """После теста вернуть модуль в дефолтное (conftest: NOAUTH=1) состояние."""
    yield
    import app.api as api
    importlib.reload(api)


def test_fail_closed_without_creds():
    """H1: пустые креды + НЕТ dev-флага → всё кроме whitelist отдаёт 503."""
    api = _reload_api({})   # ни кредов, ни TRADE4_ALLOW_NOAUTH
    c = TestClient(api.app)
    assert c.get("/st4/pairs").status_code == 503
    assert c.get("/health").status_code == 200          # whitelist открыт


def test_dev_flag_opens_service():
    """H1: dev-флаг TRADE4_ALLOW_NOAUTH=1 → сервис открыт (для тестов/локалки)."""
    api = _reload_api({"TRADE4_ALLOW_NOAUTH": "1"})
    c = TestClient(api.app)
    assert c.get("/st4/pairs").status_code == 200


def test_login_required_and_cookie_set():
    """С кредами: без cookie защищённый путь 401; /login отдаёт сессионную cookie (Strict)."""
    api = _reload_api({"TRADE4_USER": "u", "TRADE4_PASS": "p", "TRADE4_SECRET": "x" * 32})
    c = TestClient(api.app)
    assert c.get("/st4/pairs").status_code == 401
    r = c.post("/login", json={"username": "u", "password": "p"})
    assert r.status_code == 200
    sc = r.headers.get("set-cookie", "")
    assert "trade4_session=" in sc and "samesite=strict" in sc.lower() and "httponly" in sc.lower()
    assert "secure" in sc.lower()
    # с полученной cookie доступ есть. secure-cookie TestClient не шлёт по http://testserver —
    # передаём явно через cookies= (на проде https этого не нужно).
    tok = r.cookies.get("trade4_session")
    assert c.get("/st4/pairs", cookies={"trade4_session": tok}).status_code == 200


def test_login_wrong_password_401():
    api = _reload_api({"TRADE4_USER": "u", "TRADE4_PASS": "p", "TRADE4_SECRET": "x" * 32})
    c = TestClient(api.app)
    assert c.post("/login", json={"username": "u", "password": "WRONG"}).status_code == 401


def test_rate_limit_after_many_fails():
    """H3: >_LOGIN_MAX_FAILS неудачных входов с IP → 429."""
    api = _reload_api({"TRADE4_USER": "u", "TRADE4_PASS": "p", "TRADE4_SECRET": "x" * 32})
    c = TestClient(api.app)
    for _ in range(api._LOGIN_MAX_FAILS):
        assert c.post("/login", json={"username": "u", "password": "bad"}).status_code == 401
    # следующий — заблокирован, даже с ВЕРНЫМ паролем (защита от перебора)
    assert c.post("/login", json={"username": "u", "password": "p"}).status_code == 429


def test_csrf_cross_origin_post_rejected():
    """C2: авторизованный POST с чужим Origin → 403."""
    api = _reload_api({"TRADE4_USER": "u", "TRADE4_PASS": "p", "TRADE4_SECRET": "x" * 32})
    c = TestClient(api.app)
    r = c.post("/login", json={"username": "u", "password": "p"})    # получить cookie
    ck = {"trade4_session": r.cookies.get("trade4_session")}          # secure → передаём явно
    # same-origin (Sec-Fetch-Site) — проходит (не 403)
    r_ok = c.post("/st5/control/trading?on=false", cookies=ck, headers={"sec-fetch-site": "same-origin"})
    assert r_ok.status_code != 403
    # cross-site — отклонён
    r_bad = c.post("/st5/control/trading?on=false", cookies=ck, headers={"sec-fetch-site": "cross-site"})
    assert r_bad.status_code == 403
    # чужой Origin без Sec-Fetch-Site — отклонён
    r_orig = c.post("/st5/control/trading?on=false", cookies=ck, headers={"origin": "http://evil.example"})
    assert r_orig.status_code == 403


def test_csrf_allows_non_browser_clients():
    """C2: запрос без Origin/Sec-Fetch-Site (curl/скрипт) не блокируется — CSRF только из браузера."""
    api = _reload_api({"TRADE4_USER": "u", "TRADE4_PASS": "p", "TRADE4_SECRET": "x" * 32})
    c = TestClient(api.app)
    r = c.post("/login", json={"username": "u", "password": "p"})
    ck = {"trade4_session": r.cookies.get("trade4_session")}
    assert c.post("/st5/control/trading?on=false", cookies=ck).status_code != 403
