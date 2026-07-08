"""St8Session — сервисный слой «дивидендного набега».

Держит реестр бумаг, тянет дивидендный календарь и дневные цены с MOEX ISS, ведёт движки
по тикерам, строит НАГЛЯДНЫЙ КАЛЕНДАРЬ точек входа/выхода (build_calendar). Событийный
цикл (daily tick) проверяет: для каждой бумаги — не наступил ли день входа (ex − N) или
выхода (ex − 1), исполняет. Учёт по кэшу (paper) или sandbox.
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import St8Config
from .engine import St8Engine, DivEvent

# ── РЕЕСТР ВСЕЛЕННОЙ (мой бэктест 08.07: N=10, без июля, t=8.2, ~23 сд/год) ──
# ядро 16 бумаг (t>1.5, >=5 событий) + lot_size (акций в лоте FORTS-спота, для нотионала).
# lot_size здесь = лотность спота TQBR (уточняется из ISS при live; дефолт 1 для дорогих).
ST8_CORE = {
    "MOEX": "Мосбиржа", "BSPB": "Банк СПб", "IRAO": "Интер РАО", "ROSN": "Роснефть",
    "MRKC": "Россети Центр", "MGNT": "Магнит", "GMKN": "Норникель", "TATN": "Татнефть",
    "SIBN": "Газпромнефть", "TATNP": "Татнефть-п", "MRKP": "Россети ЦП", "PLZL": "Полюс",
    "MAGN": "ММК", "NLMK": "НЛМК", "BELU": "НоваБев", "PHOR": "ФосАгро",
}
# опциональные (сильные, но мало событий n<5 — малый вес, мониторинг)
ST8_OPTIONAL = {"RTKM": "Ростелеком", "PIKK": "ПИК", "FEES": "Россети", "KZOS": "Казаньоргсинтез"}

ST8_TICKERS = {**ST8_CORE, **ST8_OPTIONAL}
HEDGE_SECID = "IMOEXF"        # фьючерс индекса для хеджа беты
ISS = "https://iss.moex.com/iss"
EVENTS_LEN = 60


def _iss(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "trade4-st8"})
    return json.load(urllib.request.urlopen(req, timeout=30))


class St8Session:
    def __init__(self):
        self.cfg = St8Config()
        self.engines: dict[str, St8Engine] = {}
        self.trades: list[dict] = []
        self.events: list[dict] = []
        self.enabled = {tk: (tk in ST8_CORE) for tk in ST8_TICKERS}  # ядро вкл, опц. выкл
        self.state = {"live": False, "live_intent": False}
        self.signal_view: dict[str, dict] = {}
        self._session_file = Path(__file__).resolve().parent.parent.parent / "session_state_8.json"
        self._div_cache: dict[str, list] = {}       # tk -> [(ex_date, div, div_yield)]
        self._trading_days: list[str] = []           # календарь торговых дней (из IMOEX)
        self._task = None

    def _engine(self, tk: str) -> St8Engine:
        if tk not in self.engines:
            self.engines[tk] = St8Engine(tk, self.cfg.strategy, lot_size=1)
        return self.engines[tk]

    def log_event(self, kind: str, message: str) -> None:
        self.events.append({"ts": int(time.time() * 1000), "kind": kind, "message": message})
        if len(self.events) > EVENTS_LEN:
            del self.events[0]

    # ---------- данные ISS ----------
    def _fetch_divs(self, tk: str) -> list:
        """Дивиденды тикера: [(ex_date, div, div_yield_pct)]. ex_date = registryclosedate − 1
        торг.день (T+1 гэп). div_yield по последней close перед ex."""
        if tk in self._div_cache:
            return self._div_cache[tk]
        try:
            d = _iss(f"{ISS}/securities/{tk}/dividends.json?iss.meta=off")
            dv = d.get("dividends", d)
            ci = {c: i for i, c in enumerate(dv["columns"])}
            out = []
            for r in dv["data"]:
                rc = r[ci["registryclosedate"]]; val = r[ci["value"]]
                if not rc or not val:
                    continue
                ex = self._prev_trading_day(rc)
                if ex is None:
                    continue
                px = self._price_on(tk, ex)     # close на день гэпа (для дивдоходности)
                dy = (val / px * 100) if px else 0.0
                out.append((ex, float(val), round(dy, 2)))
            self._div_cache[tk] = out
            return out
        except Exception as e:  # noqa: BLE001
            self.log_event("warn", f"{tk}: дивиденды не получены: {str(e)[:80]}")
            return []

    def _load_trading_days(self, since: str) -> None:
        """Календарь торговых дней MOEX (по IMOEX-индексу) с since. ISS отдаёт по 100/страница
        → листаем через start до конца."""
        days, start = [], 0
        try:
            while True:
                d = _iss(f"{ISS}/history/engines/stock/markets/index/securities/IMOEX.json"
                         f"?iss.meta=off&from={since}&start={start}&history.columns=TRADEDATE")
                rows = [r[0] for r in d["history"]["data"] if r and r[0]]
                if not rows:
                    break
                days += rows
                if len(rows) < 100:
                    break
                start += len(rows)
            self._trading_days = days
        except Exception as e:  # noqa: BLE001
            self.log_event("warn", f"торговый календарь не получен: {str(e)[:60]}")

    def _prev_trading_day(self, d: str) -> str | None:
        """Торговый день СТРОГО перед d (день гэпа = регдата − 1 торг.день)."""
        if not self._trading_days:
            return None
        prev = [t for t in self._trading_days if t < d]
        return prev[-1] if prev else None

    def _price_on(self, tk: str, d: str) -> float | None:
        """Close акции на дату (для расчёта дивдоходности). Кэш не держим — редкий вызов."""
        try:
            r = _iss(f"{ISS}/history/engines/stock/markets/shares/securities/{tk}.json"
                     f"?iss.meta=off&from={d}&till={d}&history.columns=CLOSE")
            data = r["history"]["data"]
            return float(data[0][0]) if data and data[0][0] else None
        except Exception:  # noqa: BLE001
            return None

    # ---------- КАЛЕНДАРЬ ВХОДОВ/ВЫХОДОВ (наглядно) ----------
    def build_calendar(self, days_ahead: int = 120, days_back: int = 30) -> list[dict]:
        """Расписание всех точек входа/выхода: для каждого предстоящего (и недавнего)
        дивидендного события — тикер, ex-дата, ДЕНЬ ВХОДА (ex − N), ДЕНЬ ВЫХОДА (ex − M),
        дивиденд, дивдоходность, статус. Отсортировано по дате входа. Июль помечается.
        Это наглядная таблица «что и когда покупать/продавать»."""
        s = self.cfg.strategy
        today = date.today().isoformat()
        lo = (date.today() - timedelta(days=days_back)).isoformat()
        hi = (date.today() + timedelta(days=days_ahead)).isoformat()
        if not self._trading_days:
            self._load_trading_days(lo)
        rows = []
        for tk, name in ST8_TICKERS.items():
            if not self.enabled.get(tk, False):
                continue
            for ex, div, dy in self._fetch_divs(tk):
                if not (lo <= ex <= hi):
                    continue
                is_july = ex[5:7] == "07"
                # дни входа/выхода по торговому календарю
                entry_d = exit_d = None
                if ex in self._trading_days:
                    ex_i = self._trading_days.index(ex)
                    if ex_i - s.entry_days_before >= 0:
                        entry_d = self._trading_days[ex_i - s.entry_days_before]
                    if ex_i - s.exit_offset_days >= 0:
                        exit_d = self._trading_days[ex_i - s.exit_offset_days]
                # статус
                if is_july and s.skip_july:
                    status = "пропуск (июль)"
                elif dy < s.min_div_yield_pct:
                    status = f"пропуск (дивдох {dy}% < {s.min_div_yield_pct}%)"
                elif exit_d and exit_d < today:
                    status = "прошло"
                elif entry_d and entry_d <= today <= (exit_d or ex):
                    status = "🔵 В ОКНЕ (держим/входим)"
                elif entry_d and today < entry_d:
                    status = "предстоит"
                else:
                    status = "—"
                rows.append({
                    "ticker": tk, "name": name, "ex_date": ex,
                    "entry_date": entry_d, "exit_date": exit_d,
                    "div": div, "div_yield_pct": dy,
                    "july": is_july, "status": status,
                })
        rows.sort(key=lambda r: (r["entry_date"] or r["ex_date"]))
        return rows

    # ---------- снимок ----------
    def snapshot(self) -> dict:
        net = sum(t.get("net_pnl_rub", 0) for t in self.trades)
        return {
            "strategy": "st8", "live": self.state["live"], "mode": self.cfg.mode,
            "account_id": self.cfg.account_id or None,
            "trading_enabled": self.cfg.trading_enabled,
            "tickers_enabled": [tk for tk, on in self.enabled.items() if on],
            "open_positions": [
                {"ticker": tk, "entry": e.position.entry_date, "ex": e.position.ex_date,
                 "lots": e.position.lots}
                for tk, e in self.engines.items() if e.position is not None
            ],
            "net_pnl_rub": round(net),
            "trades_count": len(self.trades),
            "strategy_cfg": self.cfg.strategy.model_dump(),
            "events": self.events[-20:],
        }

    def save_session(self) -> None:
        try:
            data = {"config": self.cfg.model_dump(), "trades": self.trades,
                    "enabled": self.enabled, "state": self.state}
            self._session_file.write_text(json.dumps(data, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass

    def load_session(self) -> bool:
        if not self._session_file.exists():
            return False
        try:
            d = json.loads(self._session_file.read_text())
        except Exception:  # noqa: BLE001
            return False
        self.trades = d.get("trades", [])
        en = d.get("enabled") or {}
        self.enabled = {tk: bool(en.get(tk, tk in ST8_CORE)) for tk in ST8_TICKERS}
        self.state.update(d.get("state") or {})
        cfg = d.get("config")
        if cfg:
            try:
                self.cfg = St8Config(**cfg)
            except Exception:  # noqa: BLE001
                pass
        return True
