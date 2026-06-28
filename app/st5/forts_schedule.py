"""Расписание торговой сессии FORTS по МСК — backend-порт s4fortsSession() из dashboard.html.

Чистые функции (без I/O) для планировщика уведомлений: «биржа открыта/закрыта», минута
открытия. Часы сессии совпадают с UI (единый источник истины — при правке сверять оба места).

FORTS (пн–пт):
  09:00–10:00  утренняя сессия
  10:00–14:00  основная
  14:00–14:05  клиринг (нет торгов)
  14:05–18:45  основная (продолжение)
  18:45–19:05  вечерний клиринг
  19:05–23:50  вечерняя сессия
  иначе        закрыто; сб/вс — выходной
"""
from __future__ import annotations

import time

OPEN_MIN = 9 * 60          # 09:00 — открытие утренней сессии


def msk_minute_dow(ts_sec: float | None = None) -> tuple[int, int, int]:
    """(минута дня по МСК, секунда дня, день недели) для unix-секунд. МСК = UTC+3, без DST.

    dow: 0=вс … 6=сб (как Date.getDay() в JS — для совместимости с UI-логикой выходных)."""
    if ts_sec is None:
        ts_sec = time.time()
    t = time.gmtime(ts_sec + 3 * 3600)           # сдвиг на МСК
    minute = t.tm_hour * 60 + t.tm_min
    sec = t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec
    dow = (t.tm_wday + 1) % 7                     # tm_wday: пн=0…вс=6 → JS: вс=0…сб=6
    return minute, sec, dow


def forts_kind(minute: int, dow: int) -> str:
    """Состояние сессии: 'live' (торги идут) | 'warn' (клиринг) | 'closed'."""
    if dow == 0 or dow == 6:                      # вс / сб
        return "closed"
    if 14 * 60 <= minute < 14 * 60 + 5:           # дневной клиринг
        return "warn"
    if 18 * 60 + 45 <= minute < 19 * 60 + 5:      # вечерний клиринг
        return "warn"
    if 9 * 60 <= minute < 10 * 60:
        return "live"
    if (10 * 60 <= minute < 14 * 60) or (14 * 60 + 5 <= minute < 18 * 60 + 45):
        return "live"
    if 19 * 60 + 5 <= minute < 23 * 60 + 50:
        return "live"
    return "closed"


def is_trading_day(dow: int) -> bool:
    """Будний день (биржа работает). dow: 0=вс…6=сб."""
    return 1 <= dow <= 5


def session_open(ts_sec: float | None = None) -> bool:
    """Идут ли торги прямо сейчас (kind == 'live')."""
    minute, _sec, dow = msk_minute_dow(ts_sec)
    return forts_kind(minute, dow) == "live"
