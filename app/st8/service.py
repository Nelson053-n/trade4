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

from dataclasses import asdict

from .config import St8Config
from .engine import St8Engine, DivEvent


def _trade_dict(tr) -> dict:
    """St8Trade → dict для журнала (net_pnl_rub ключ для аудита/сверки)."""
    return asdict(tr)

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


def _iss_live_price(tk: str) -> dict | None:
    """Живые котировки акции TQBR: last/bid/offer/оборот/время. None если рынок закрыт/нет."""
    try:
        d = _iss(f"{ISS}/engines/stock/markets/shares/boards/TQBR/securities/{tk}.json"
                 "?iss.meta=off&iss.only=marketdata&marketdata.columns=LAST,BID,OFFER,VALTODAY,UPDATETIME")
        row = d["marketdata"]["data"]
        if not row or row[0][0] is None:
            return None
        r = row[0]
        return {"last": r[0], "bid": r[1], "offer": r[2], "val": r[3], "time": r[4]}
    except Exception:  # noqa: BLE001
        return None


def _iss_hedge_price() -> float | None:
    """Живая цена фьючерса IMOEXF (для хедж-ноги)."""
    try:
        d = _iss(f"{ISS}/engines/futures/markets/forts/securities/{HEDGE_SECID}.json"
                 "?iss.meta=off&iss.only=marketdata&marketdata.columns=LAST,LASTSETTLEPRICE")
        row = d["marketdata"]["data"]
        if not row:
            return None
        return row[0][0] or row[0][1]
    except Exception:  # noqa: BLE001
        return None


def _iss_lot_size(tk: str) -> int:
    """Лотность акции TQBR (для расчёта нотионала). Дефолт 1."""
    try:
        d = _iss(f"{ISS}/engines/stock/markets/shares/boards/TQBR/securities/{tk}.json"
                 "?iss.meta=off&iss.only=securities&securities.columns=LOTSIZE")
        row = d["securities"]["data"]
        return int(row[0][0]) if row and row[0][0] else 1
    except Exception:  # noqa: BLE001
        return 1


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
        self._lot_cache: dict[str, int] = {}         # tk -> лотность (для нотионала)
        self.market: dict[str, dict] = {}            # tk -> живые котировки (last/bid/offer/время)
        self.hedge_px: float | None = None           # живая цена IMOEXF
        self.new_dividends: list[dict] = []          # свежеобъявленные дивиденды (мониторинг)
        self.exec_anchor: dict | None = None         # якорь аудита журнал↔счёт (кэш-истина)
        self.capital_rub: float = 0.0                # капитал sandbox-счёта (для аудита)
        self._div_seen: dict[str, str] = {}          # tk -> последняя известная ex-date (детект новых)
        self.missed: list[dict] = []                 # упущенные входы (событие было, входа нет)
        self._executor = None                        # St8Executor (ленивая инициализация)
        self._task = None

    def _engine(self, tk: str) -> St8Engine:
        if tk not in self.engines:
            lot = self._lot(tk) if self.cfg.mode == "tbank_sandbox" else 1
            self.engines[tk] = St8Engine(tk, self.cfg.strategy, lot_size=lot)
        return self.engines[tk]

    def _exec(self):
        """Ленивый St8Executor под текущий режим/счёт."""
        from .executor import St8Executor
        if self._executor is None or self._executor.account_id != self.cfg.account_id:
            self._executor = St8Executor(
                self.cfg.account_id, paper=(self.cfg.mode != "tbank_sandbox"),
                audit_cb=lambda a: self.log_event("order",
                    f"{a['op']} {a['direction']} {a['lots']}лот → {a.get('status')}"))
        return self._executor

    def log_missed(self, tk: str, day: str, ex_date: str, reason: str) -> None:
        if any(m["ticker"] == tk and m["ex_date"] == ex_date for m in self.missed):
            return
        self.missed.append({"ts": int(time.time() * 1000), "date": day,
                            "ticker": tk, "ex_date": ex_date, "reason": reason})
        if len(self.missed) > 100:
            del self.missed[0]

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

    def _lot(self, tk: str) -> int:
        if tk not in self._lot_cache:
            self._lot_cache[tk] = _iss_lot_size(tk)
        return self._lot_cache[tk]

    # ---------- ЖИВОЙ СБОР ДАННЫХ С БИРЖИ ----------
    def refresh_market(self) -> dict:
        """Подтянуть ЖИВЫЕ котировки всех включённых бумаг + фьючерс хеджа. Для анализа
        (текущие спреды bid/ask, оборот) и для исполнения по актуальной цене. Возвращает
        сводку. Рынок закрыт → last=None, помечаем."""
        got, closed = 0, 0
        for tk in ST8_TICKERS:
            if not self.enabled.get(tk, False):
                continue
            q = _iss_live_price(tk)
            if q:
                q["spread_pct"] = (round((q["offer"] - q["bid"]) / q["bid"] * 100, 3)
                                   if q.get("bid") and q.get("offer") else None)
                self.market[tk] = q
                got += 1
            else:
                closed += 1
        self.hedge_px = _iss_hedge_price()
        self.log_event("info", f"котировки обновлены: {got} бумаг live, {closed} без данных, "
                               f"IMOEXF {self.hedge_px}")
        return {"live": got, "closed": closed, "hedge_px": self.hedge_px}

    # ---------- МОНИТОРИНГ НОВЫХ ДИВИДЕНДОВ ----------
    def scan_new_dividends(self) -> list[dict]:
        """Проверить, не объявили ли эмитенты НОВЫЕ дивиденды (свежие ex-даты). Ключевое для
        событийной стратегии: эмитенты объявляют за 1-2 мес до отсечки, надо ловить сразу,
        чтобы успеть к окну входа (−10 дней). Возвращает список новых событий."""
        today = date.today().isoformat()
        found = []
        for tk in ST8_TICKERS:
            if not self.enabled.get(tk, False):
                continue
            self._div_cache.pop(tk, None)      # сбросить кэш → перекачать свежие
            for ex, div, dy in self._fetch_divs(tk):
                if ex < today:
                    continue
                prev = self._div_seen.get(tk)
                if prev is None or ex > prev:
                    is_new = ex not in [n["ex_date"] for n in self.new_dividends if n["ticker"] == tk]
                    if is_new:
                        rec = {"ticker": tk, "ex_date": ex, "div": div, "div_yield_pct": dy,
                               "detected": today}
                        self.new_dividends.append(rec)
                        found.append(rec)
                        self._div_seen[tk] = ex
                        self.log_event("signal", f"🆕 {tk}: объявлен дивиденд {div}₽ "
                                                 f"(дох {dy}%), отсечка {ex}")
        if len(self.new_dividends) > 100:
            self.new_dividends = self.new_dividends[-100:]
        return found

    # ---------- АУДИТ журнал↔счёт (кэш-истина, урок проекта) ----------
    def refresh_capital(self) -> None:
        """Капитал sandbox-счёта + якорь аудита. ЖУРНАЛ ВРЁТ — истина только кэш счёта
        (урок band/st4: журнальный P&L расходился с реальным вдвое)."""
        if self.cfg.mode != "tbank_sandbox" or not self.cfg.account_id:
            return
        try:
            from ..st4 import tbank_sandbox as sb
            pf = sb.portfolio(self.cfg.account_id)
            total = sb._q_to_float(pf.get("totalAmountCurrencies") or pf.get("totalAmountPortfolio"))
        except Exception:  # noqa: BLE001
            return
        if total and total > 0:
            self.capital_rub = float(total)
            if self.exec_anchor is None or self.exec_anchor.get("account_id") != self.cfg.account_id:
                net = sum(t.get("net_pnl_rub", 0) for t in self.trades)
                self.exec_anchor = {"account_id": self.cfg.account_id,
                                    "capital": float(total), "net": net}

    def execution_gap(self) -> float | None:
        """Δфакт счёта − Δжурнал от якоря. Отрицательный = скрытые издержки/расхождение.
        None — нет якоря/paper. Главная метрика достоверности учёта."""
        a = self.exec_anchor
        if a is None or not self.capital_rub or a.get("account_id") != self.cfg.account_id:
            return None
        net = sum(t.get("net_pnl_rub", 0) for t in self.trades)
        return round((self.capital_rub - a.get("capital", 0.0)) - (net - a.get("net", 0.0)))

    # ---------- DAILY-TICK ЦИКЛ (событийная торговля) ----------
    def _hedge_lots_for(self, tk: str, stock_px: float, stock_lots: int) -> int:
        """Сколько лотов IMOEXF шортить под нотионал позиции × hedge_ratio.
        Нотионал акции = stock_px × stock_lots × lot_size; IMOEXF пункт 10₽."""
        if not self.cfg.strategy.hedge_imoexf or not self.hedge_px:
            return 0
        eng = self._engine(tk)
        notional = stock_px * stock_lots * eng.lot_size * self.cfg.strategy.hedge_ratio
        hedge_notional = self.hedge_px * 10.0   # пункт-стоимость IMOEXF = 10₽
        return max(0, round(notional / hedge_notional)) if hedge_notional > 0 else 0

    def tick(self) -> dict:
        """Один daily-тик: скан дивидендов → котировки → для каждой бумаги проверить
        вход (ex−N) / выход (ex−1 или стоп) → исполнить. Возвращает сводку действий."""
        today = date.today().isoformat()
        acted = {"entered": [], "exited": [], "missed": 0}
        if not self._trading_days or self._trading_days[-1] < today:
            self._load_trading_days((date.today() - timedelta(days=400)).isoformat())
        self.scan_new_dividends()
        self.refresh_market()
        # все объявленные события в горизонте (для сигналов)
        events = []
        for tk in ST8_TICKERS:
            if not self.enabled.get(tk, False):
                continue
            for ex, div, dy in self._fetch_divs(tk):
                if ex >= today or (self.engines.get(tk) and self.engines[tk].position):
                    events.append(DivEvent(tk, ex, div, dy))
        # market открыт? (есть свежие котировки)
        market_open = any(q.get("last") for q in self.market.values())
        for tk in ST8_TICKERS:
            if not self.enabled.get(tk, False):
                continue
            eng = self._engine(tk)
            q = self.market.get(tk, {})
            stock_px = q.get("offer") or q.get("last")   # вход по ask (реализм)
            # ── ВЫХОД (приоритет: стоп, потом плановый) ──
            if eng.position is not None:
                sell_px = q.get("bid") or q.get("last") or eng.position.stock_entry
                stop = eng.check_stop(sell_px, self.hedge_px or eng.position.hedge_entry)
                out_day = eng.exit_day(self._trading_days)
                if stop or (out_day and today >= out_day):
                    if not market_open:
                        continue
                    reason = "stop" if stop else "exit"
                    self._exec().close(tk, eng.position.lots, sell_px,
                                       eng.position.hedge_lots, self.hedge_px or 0)
                    tr = eng.close(today, sell_px, self.hedge_px or eng.position.hedge_entry, reason)
                    self.trades.append(_trade_dict(tr))
                    acted["exited"].append(tk)
                    self.log_event("exit", f"{tk}: выход ({reason}) net {tr.net_pnl_rub:+.0f}₽")
                continue
            # ── ВХОД (день входа = ex−N) ──
            ev = eng.entry_signal(today, events, self._trading_days)
            if ev is None:
                continue
            if not (self.cfg.trading_enabled and market_open and stock_px):
                self.log_missed(tk, today, ev.ex_date,
                                "торговля выкл" if not self.cfg.trading_enabled else "рынок закрыт/нет цены")
                acted["missed"] += 1
                continue
            # фильтр ликвидности: широкий спред съедает edge (MRKC/MRKP/BELU дороги)
            max_sp = self.cfg.strategy.max_spread_pct
            sp = q.get("spread_pct")
            if max_sp > 0 and sp is not None and sp > max_sp:
                self.log_missed(tk, today, ev.ex_date, f"спред {sp:.2f}% > {max_sp}% (дорогое исполнение)")
                acted["missed"] += 1
                continue
            hlots = self._hedge_lots_for(tk, stock_px, self.cfg.strategy.quantity_lots)
            try:
                r = self._exec().open(tk, self.cfg.strategy.quantity_lots, stock_px,
                                      hlots, self.hedge_px or 0)
                eng.open(today, ev, stock_px, self.hedge_px or 0, r.get("hedge_filled", 0))
                acted["entered"].append(tk)
                self.log_event("position", f"{tk}: ВХОД {r['stock_filled']}лот @ {stock_px} "
                                           f"+ хедж {r.get('hedge_filled',0)} IMOEXF (отсечка {ev.ex_date})")
            except Exception as e:  # noqa: BLE001
                self.log_missed(tk, today, ev.ex_date, f"брокер: {str(e)[:80]}")
                acted["missed"] += 1
        self.refresh_capital()
        self.save_session()
        return acted

    async def run_live(self) -> None:
        import asyncio
        self.state["live"] = True
        while self.state["live"]:
            try:
                await asyncio.to_thread(self.tick)
            except Exception as e:  # noqa: BLE001
                self.log_event("warn", f"тик не удался: {str(e)[:100]}")
            await asyncio.sleep(self.cfg.poll_seconds)

    def start_live(self) -> None:
        import asyncio
        if self.state["live"]:
            return
        self.state["live"] = True
        self.state["live_intent"] = True
        self._task = asyncio.create_task(self.run_live())
        self.log_event("info", f"ST8 live запущен ({self.cfg.mode}, дивидендный набег)")
        self.save_session()

    def stop_live(self) -> None:
        self.state["live"] = False
        self.state["live_intent"] = False
        self.log_event("info", "ST8 остановлен")
        self.save_session()

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
            "capital_rub": round(self.capital_rub) or None,
            "execution_gap_rub": self.execution_gap(),      # аудит журнал↔счёт (кэш-истина)
            "new_dividends": self.new_dividends[-15:],        # свежеобъявленные (мониторинг)
            "hedge_px": self.hedge_px,
            "market_quotes": len(self.market),
            "missed": self.missed[-15:],
            "strategy_cfg": self.cfg.strategy.model_dump(),
            "events": self.events[-20:],
        }

    def market_view(self) -> list[dict]:
        """Живые котировки включённых бумаг для анализа: last/bid/offer/спред/оборот/лот."""
        out = []
        for tk, name in ST8_TICKERS.items():
            if not self.enabled.get(tk, False):
                continue
            q = self.market.get(tk, {})
            out.append({"ticker": tk, "name": name, "last": q.get("last"),
                        "bid": q.get("bid"), "offer": q.get("offer"),
                        "spread_pct": q.get("spread_pct"), "val_rub": q.get("val"),
                        "lot": self._lot(tk) if tk in self._lot_cache else None,
                        "time": q.get("time")})
        return out

    def save_session(self) -> None:
        try:
            data = {"config": self.cfg.model_dump(), "trades": self.trades,
                    "enabled": self.enabled, "state": self.state,
                    "exec_anchor": self.exec_anchor, "new_dividends": self.new_dividends[-100:],
                    "div_seen": self._div_seen, "missed": self.missed[-100:]}
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
        self.exec_anchor = d.get("exec_anchor") or None
        self.new_dividends = list(d.get("new_dividends") or [])
        self._div_seen = dict(d.get("div_seen") or {})
        self.missed = list(d.get("missed") or [])
        cfg = d.get("config")
        if cfg:
            try:
                self.cfg = St8Config(**cfg)
            except Exception:  # noqa: BLE001
                pass
        return True
