"""Telegram-уведомления ST5 — только ИСХОДЯЩИЕ (бот не принимает команд).

Тонкий клиент Bot API на httpx (уже в зависимостях). Принцип: уведомление НИКОГДА не ломает
торговый цикл — любая ошибка отправки проглатывается (логируется через on_error-callback).

Токен бота — секрет: только в env TG_BOT_TOKEN или файле app/st5/.tg_bot_token (0600, в .gitignore),
НИКОГДА в session-файле/snapshot (наружу отдаётся лишь булев token_set), по аналогии с TBANK_TOKEN.
chat_id и флаги уведомлений — не секрет, живут в St5Config.notify (персистятся).
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx

# Файл-хранилище токена бота (переживает рестарт). В .gitignore, права 0600.
_TOKEN_FILE = Path(__file__).with_name(".tg_bot_token")
_API = "https://api.telegram.org"


def save_bot_token(token: str) -> None:
    """Сохранить токен бота в файл (0600) + env. Пусто → удалить."""
    token = (token or "").strip()
    if token:
        os.environ["TG_BOT_TOKEN"] = token
        try:
            _TOKEN_FILE.write_text(token)
            _TOKEN_FILE.chmod(0o600)
        except Exception:  # noqa: BLE001  не удалось записать — токен хотя бы в env
            pass
    else:
        os.environ.pop("TG_BOT_TOKEN", None)
        _TOKEN_FILE.unlink(missing_ok=True)


def load_bot_token() -> bool:
    """Подтянуть токен из файла в env при старте. True — если токен есть."""
    if os.environ.get("TG_BOT_TOKEN", "").strip():
        return True
    try:
        if _TOKEN_FILE.exists():
            tok = _TOKEN_FILE.read_text().strip()
            if tok:
                os.environ["TG_BOT_TOKEN"] = tok
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def has_bot_token() -> bool:
    """Есть ли токен (в env или файле) — без раскрытия самого секрета."""
    return bool(os.environ.get("TG_BOT_TOKEN", "").strip()) or _TOKEN_FILE.exists()


class TelegramNotifier:
    """Отправщик в Telegram. Конфиг (chat_id/enabled) читается лениво из St5Config через cfg_cb,
    чтобы изменения настроек в UI применялись без пересоздания объекта."""

    def __init__(self, cfg_cb, on_error=None):
        self._cfg_cb = cfg_cb          # () -> St5NotifyConfig (enabled, chat_id, флаги)
        self._on_error = on_error      # callback(str) — лог ошибки (обычно log_event "warn")

    async def send(self, text: str) -> bool:
        """Отправить сообщение. True — отправлено. Любая ошибка → False (НЕ исключение).

        Гейты (любой не пройден → тихо False, без ошибки): notify.enabled, наличие токена и
        chat_id. parse_mode=HTML — текст уже должен быть собран с экранированием."""
        cfg = self._cfg_cb()
        token = os.environ.get("TG_BOT_TOKEN", "").strip()
        if not cfg.enabled or not token or not cfg.chat_id:
            return False
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    f"{_API}/bot{token}/sendMessage",
                    json={"chat_id": cfg.chat_id, "text": text,
                          "parse_mode": "HTML", "disable_web_page_preview": True},
                )
            if r.status_code != 200:
                self._fail(f"Telegram HTTP {r.status_code}: {r.text[:200]}")
                return False
            return True
        except Exception as e:  # noqa: BLE001  сеть/таймаут — не ломаем торговый цикл
            self._fail(f"Telegram отправка не удалась: {e}")
            return False

    def _fail(self, msg: str) -> None:
        if self._on_error:
            try:
                self._on_error(msg)
            except Exception:  # noqa: BLE001
                pass


def esc(s) -> str:
    """Экранировать текст для parse_mode=HTML Telegram (& < >)."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
