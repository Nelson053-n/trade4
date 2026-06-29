"""Хранилище версий стратегии ST5: per-pair параметры + метрики бэктеста + дата.

Каждая сохранённая стратегия — JSON-файл out/st5_strategies/<id>.json. Позволяет видеть историю
(когда меняли, на что, какой бэктест) и откатываться. Параметры применяет St5Session.apply_overrides
(хранилище только хранит — применение/гейт по позициям живёт в сессии).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

_STORE_DIR = Path(__file__).resolve().parent.parent.parent / "out" / "st5_strategies"


def _ensure_dir() -> None:
    _STORE_DIR.mkdir(parents=True, exist_ok=True)


def save_strategy(name: str, params: dict, backtest: dict | None = None,
                  window: str = "", note: str = "", source: str = "manual",
                  ts_ms: int | None = None) -> str:
    """Сохранить стратегию. params: {pid: {param: value}}. backtest: {pid: метрики}.
    Возвращает id (по времени создания). ts_ms — для тестов/детерминизма."""
    _ensure_dir()
    created = int(ts_ms if ts_ms is not None else time.time() * 1000)
    sid = f"s{created}"
    rec = {
        "id": sid,
        "name": name or sid,
        "created_ts": created,
        "params": params,
        "backtest": backtest or {},
        "window": window,
        "note": note,
        "source": source,
    }
    (_STORE_DIR / f"{sid}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2))
    return sid


def list_strategies() -> list[dict]:
    """Список сохранённых стратегий (новые сверху). Лёгкая мета без полной нагрузки —
    но файлы маленькие, отдаём целиком для таблицы UI."""
    if not _STORE_DIR.exists():
        return []
    out = []
    for f in _STORE_DIR.glob("s*.json"):
        try:
            out.append(json.loads(f.read_text()))
        except Exception:  # noqa: BLE001  битый файл не должен ронять список
            continue
    out.sort(key=lambda r: r.get("created_ts", 0), reverse=True)
    return out


def load_strategy(sid: str) -> dict | None:
    """Полная запись по id или None."""
    f = _STORE_DIR / f"{sid}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:  # noqa: BLE001
        return None


def delete_strategy(sid: str) -> bool:
    f = _STORE_DIR / f"{sid}.json"
    if f.exists():
        f.unlink()
        return True
    return False
