"""Веб-оболочка восстановления: без lifespan торгового приложения и доступа к движкам."""
from contextlib import asynccontextmanager
from starlette.responses import JSONResponse, Response
from app.api import app


@asynccontextmanager
async def recovery_lifespan(_app):
    yield


app.router.lifespan_context = recovery_lifespan
NOTICE = ('<div style="padding:16px;background:#613d04;color:#fff;font:15px/1.5 sans-serif;'
          'position:relative;z-index:99999">Режим восстановления. Торговля отключена. '
          'Рабочие состояния и доступ к брокеру со старого сервера пока не восстановлены. '
          'Торговые данные недоступны.</div>')


@app.middleware('http')
async def recovery_gate(request, call_next):
    if request.url.path == '/health':
        return JSONResponse({'ok': True, 'recovery_mode': True, 'trading_ready': False})
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.url.path not in ('/login', '/logout'):
        return JSONResponse({'detail': 'Торговые действия отключены в режиме восстановления'}, status_code=503)
    if request.url.path.startswith(('/st4/', '/st5/', '/st6/', '/st7/', '/st8/', '/st9/', '/reconcile/')):
        return JSONResponse({'detail': 'Рабочие торговые данные не восстановлены'}, status_code=503)
    response = await call_next(request)
    if 'text/html' in response.headers.get('content-type', ''):
        body = b''.join([chunk async for chunk in response.body_iterator]).decode('utf-8')
        body = body.replace('<body>', '<body>' + NOTICE, 1)
        headers = dict(response.headers)
        headers.pop('content-length', None)
        return Response(body, status_code=response.status_code, headers=headers)
    return response
