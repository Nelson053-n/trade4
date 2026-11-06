"""Восстановленный сервис st8/st9: прежние счета песочницы и явный статус истории."""
import json
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.responses import JSONResponse, Response

from app.api import app, health, lifespan, ST8, ST9


@asynccontextmanager
async def sandbox_lifespan(application):
    # Ошибочная подмена подготовленного режима не должна запустить реальный контур.
    for number in (8, 9):
        path = Path(__file__).resolve().parent / f'session_state_{number}.json'
        config = json.loads(path.read_text())['config']
        if config['mode'] != 'tbank_sandbox' or not config['account_id']:
            raise RuntimeError('Восстановление разрешено только на заданных sandbox-счетах')
    async with lifespan(application):
        yield


app.router.lifespan_context = sandbox_lifespan
NOTICE = ('<div style="padding:12px;background:#613d04;color:#fff;font:14px/1.5 sans-serif;'
          'position:relative;z-index:99999">Песочница T-Bank: виртуальные деньги. '
          'Полные старые журналы роботов не восстановлены; новые записи ведутся после восстановления. '
          'История операций брокера сохранена, показатели счёта доступны в Info.</div>')


@app.middleware('http')
async def sandbox_status(request, call_next):
    if request.url.path == '/health':
        ready = all(s.cfg.mode == 'tbank_sandbox' and s.state.get('live') and s.last_tick_ts
                    for s in (ST8, ST9))
        return JSONResponse({**health(), 'recovery_mode': False, 'trading_ready': bool(ready),
                             'trading_mode': 'tbank_sandbox', 'history_recovered': False})
    response = await call_next(request)
    if request.url.path == '/' and 'text/html' in response.headers.get('content-type', ''):
        body = b''.join([chunk async for chunk in response.body_iterator]).decode('utf-8')
        headers = dict(response.headers)
        headers.pop('content-length', None)
        return Response(body.replace('<body>', '<body>' + NOTICE, 1), status_code=response.status_code,
                        headers=headers)
    return response
