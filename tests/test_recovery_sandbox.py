"""Гейт запуска восстановленной песочницы до создания торговых задач."""
import asyncio
import importlib.util
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    from app.api import app
    monkeypatch.setattr(app.router, 'lifespan_context', app.router.lifespan_context)
    monkeypatch.setattr(app, 'user_middleware', list(app.user_middleware))
    monkeypatch.setattr(app, 'middleware_stack', app.middleware_stack)
    path = Path(__file__).resolve().parents[1] / 'infra/sandbox_server.py'
    spec = importlib.util.spec_from_file_location('sandbox_recovery_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, '__file__', str(tmp_path / 'sandbox_server.py'))
    for number in (8, 9):
        (tmp_path / f'session_state_{number}.json').write_text(json.dumps({
            'config': {'mode': 'tbank_sandbox', 'account_id': f'sandbox-{number}'}}))
    return module, tmp_path


def test_real_mode_stops_startup_before_any_live_task(sandbox, monkeypatch):
    module, root = sandbox
    (root / 'session_state_9.json').write_text(json.dumps({
        'config': {'mode': 'tbank_real', 'account_id': 'real-account'}}))
    called = []
    @asynccontextmanager
    async def original(app):
        called.append(True)
        yield
    monkeypatch.setattr(module, 'lifespan', original)
    async def start():
        async with module.sandbox_lifespan(None):
            pass
    with pytest.raises(RuntimeError, match='только'):
        asyncio.run(start())
    assert not called


def test_valid_sandbox_runs_original_lifespan(sandbox, monkeypatch):
    module, root = sandbox
    called = []
    @asynccontextmanager
    async def original(app):
        called.append('start')
        yield
        called.append('stop')
    monkeypatch.setattr(module, 'lifespan', original)
    async def start():
        async with module.sandbox_lifespan(None):
            assert called == ['start']
    asyncio.run(start())
    assert called == ['start', 'stop']


def test_health_is_not_ready_until_both_engines_complete_a_tick(sandbox, monkeypatch):
    module, _ = sandbox
    sessions = [SimpleNamespace(cfg=SimpleNamespace(mode='tbank_sandbox'),
                               state={'live': True}, last_tick_ts=10),
                SimpleNamespace(cfg=SimpleNamespace(mode='tbank_sandbox'),
                               state={'live': True}, last_tick_ts=0)]
    monkeypatch.setattr(module, 'ST8', sessions[0])
    monkeypatch.setattr(module, 'ST9', sessions[1])
    monkeypatch.setattr(module, 'health', lambda: {'ok': True})
    request = SimpleNamespace(url=SimpleNamespace(path='/health'))
    response = asyncio.run(module.sandbox_status(request, None))
    assert json.loads(response.body)['trading_ready'] is False
    sessions[1].last_tick_ts = 10
    response = asyncio.run(module.sandbox_status(request, None))
    body = json.loads(response.body)
    assert body['trading_ready'] is True
    assert body['trading_mode'] == 'tbank_sandbox'
