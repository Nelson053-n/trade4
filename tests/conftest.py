"""Тестовая конфигурация. Auth в app.api вычисляется при импорте модуля; без заданных
TRADE4_USER/PASS сервис теперь fail-closed (503). Тесты ходят к API без авторизации, поэтому
выставляем явный dev-opt-out ДО импорта app.api (на уровне сессии pytest)."""
import os

os.environ.setdefault("TRADE4_ALLOW_NOAUTH", "1")
