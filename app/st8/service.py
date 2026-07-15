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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dataclasses import asdict

from .config import St8Config
from .engine import St8Engine, DivEvent


def _trade_dict(tr) -> dict:
    """St8Trade → dict для журнала (net_pnl_rub ключ для аудита/сверки)."""
    return asdict(tr)


def eng_side_label(side: str) -> str:
    return "шорт" if side == "short" else "лонг"

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
# расширение 08.07 (широкий анализ 55 бумаг 2020-2025, выход ex-2, t>1.5 без июля):
# SELG +4.31% t=2.71, LSNGP +3.04% t=2.28, LKOH +2.29% t=1.89, NVTK +2.11% t=1.62,
# ALRS +1.34% t=1.73. Портфель 18 бумаг: n=138, +3.49%/сд, t=9.54, win 78%, ~27 сд/год.
# ВСЕ 6 ЛЕТ В ПЛЮСЕ (2020-2025). Отвергнуты: CHMF −1.23, RASP −1.30, AKRN +0.05 (шум),
# DIAS +5.13 (n=4, мониторить). TRMK/MRKU/MSRS/UPRO/SFIN/LSRG — t<1.5.
ST8_EXTENDED = {"SELG": "Селигдар", "LSNGP": "Ленэнерго-п", "LKOH": "ЛУКОЙЛ",
                "NVTK": "НОВАТЭК", "ALRS": "АЛРОСА"}

ST8_TICKERS = {**ST8_CORE, **ST8_OPTIONAL, **ST8_EXTENDED}
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


def _iss_lot_size(tk: str) -> int | None:
    """Лотность акции TQBR (для расчёта нотионала). None при сбое ISS —
    НЕ кэшировать сбой как «1» (ломает сайзинг 10-лотовых бумаг типа NLMK)."""
    try:
        d = _iss(f"{ISS}/engines/stock/markets/shares/boards/TQBR/securities/{tk}.json"
                 "?iss.meta=off&iss.only=securities&securities.columns=LOTSIZE")
        row = d["securities"]["data"]
        return int(row[0][0]) if row and row[0][0] else 1
    except Exception:  # noqa: BLE001
        return None


def _iss_futures_for_asset(asset: str) -> list[tuple[str, str]]:
    """Все торгуемые фьючерсы FORTS на актив: [(SECID, LASTTRADEDATE)] по дате экспирации."""
    try:
        d = _iss(f"{ISS}/engines/futures/markets/forts/securities.json"
                 "?iss.meta=off&securities.columns=SECID,ASSETCODE,LASTTRADEDATE")
        ci = {c: i for i, c in enumerate(d["securities"]["columns"])}
        out = []
        for r in d["securities"]["data"]:
            if (r[ci["ASSETCODE"]] or "").upper() == asset.upper() and r[ci["LASTTRADEDATE"]]:
                out.append((r[ci["SECID"]], r[ci["LASTTRADEDATE"]]))
        return sorted(out, key=lambda x: x[1])
    except Exception:  # noqa: BLE001
        return []


def _iss_fut_quote(secid: str) -> dict | None:
    """Живые котировки фьючерса FORTS: last/bid/offer (для исполнения с плечом)."""
    try:
        d = _iss(f"{ISS}/engines/futures/markets/forts/securities/{secid}.json"
                 "?iss.meta=off&iss.only=marketdata&marketdata.columns=LAST,BID,OFFER,LASTSETTLEPRICE")
        row = d["marketdata"]["data"]
        if not row:
            return None
        r = row[0]
        last = r[0] or r[3]
        if not last:
            return None
        return {"last": last, "bid": r[1] or last, "offer": r[2] or last}
    except Exception:  # noqa: BLE001
        return None


class St8Session:
    def __init__(self):
        self.cfg = St8Config()
        self.engines: dict[str, St8Engine] = {}
        self.trades: list[dict] = []
        self.events: list[dict] = []
        self.enabled = {tk: (tk in ST8_CORE or tk in ST8_EXTENDED) for tk in ST8_TICKERS}  # ядро+расширение вкл, опц. выкл
        self.ticker_overrides: dict[str, dict] = {}  # tk -> {entry_days_before,exit_offset_days,min_div_yield_pct,stop_loss_pct,entry_notional_rub} (персист, поверх глобала)
        self.state = {"live": False, "live_intent": False}
        self.signal_view: dict[str, dict] = {}
        self._session_file = Path(__file__).resolve().parent.parent.parent / "session_state_8.json"
        self._div_cache: dict[str, list] = {}       # tk -> [(ex_date, div, div_yield)]
        self._trading_days: list[str] = []           # календарь: история IMOEX + проекция будущего
        self._tdays_date: str = ""                   # дата последней загрузки календаря
        self._lot_cache: dict[str, int] = {}         # tk -> лотность (для нотионала)
        self.market: dict[str, dict] = {}            # tk -> живые котировки (last/bid/offer/время)
        self.hedge_px: float | None = None           # живая цена IMOEXF
        self.new_dividends: list[dict] = []          # свежеобъявленные дивиденды (мониторинг)
        self.exec_anchor: dict | None = None         # якорь аудита журнал↔счёт (кэш-истина)
        self.capital_rub: float = 0.0                # капитал sandbox-счёта (для аудита)
        self._div_seen: dict[str, str] = {}          # tk -> последняя известная ex-date (детект новых)
        self.missed: list[dict] = []                 # упущенные входы (событие было, входа нет)
        self._sleeping: list[str] = []               # кэш спящих бумаг (обновляется в tick)
        self._fut_cache: dict[str, tuple] = {}       # tk -> (secid|None, expiry, дата_резолва)
        self._pv_cache: dict[str, float] = {}        # secid фьюча -> пункт-стоимость ₽
        self.fut_market: dict[str, dict] = {}        # tk -> живые котировки ЕГО фьючерса
        self._executor = None                        # St8Executor (ленивая инициализация)
        self.last_tick_ts: int = 0                   # мс; наблюдаемость живости цикла
        self._task = None

    # per-ticker параметры калибровки, которые можно переопределять поверх глобала
    PER_TICKER_KEYS = ("entry_days_before", "exit_offset_days", "min_div_yield_pct",
                       "stop_loss_pct", "entry_notional_rub")

    def _p(self, tk: str, key: str):
        """Эффективное значение параметра для тикера: per-ticker оверрайд или глобал."""
        ov = self.ticker_overrides.get(tk)
        if ov and key in ov and ov[key] is not None:
            return ov[key]
        return getattr(self.cfg.strategy, key)

    def _engine(self, tk: str) -> St8Engine:
        if tk not in self.engines:
            lot = self._lot(tk) if self.cfg.mode in ("tbank_sandbox", "tbank_real") else 1
            # per-ticker оверрайды: даём движку КОПИЮ strategy с наложенными значениями,
            # чтобы движковые методы (exit_day по exit_offset_days, check_stop по stop_loss_pct)
            # видели персональные параметры тикера
            strat = self.cfg.strategy
            ov = self.ticker_overrides.get(tk)
            if ov:
                strat = self.cfg.strategy.model_copy(update={
                    k: ov[k] for k in self.PER_TICKER_KEYS
                    if k in ov and ov[k] is not None and hasattr(self.cfg.strategy, k)})
            self.engines[tk] = St8Engine(tk, strat, lot_size=lot)
        return self.engines[tk]

    def _rebuild_engine(self, tk: str) -> None:
        """Пересоздать движок тикера под новые оверрайды, СОХРАНИВ открытую позицию."""
        old = self.engines.get(tk)
        pos = old.position if old else None
        self.engines.pop(tk, None)
        eng = self._engine(tk)
        if pos is not None:
            eng.position = pos

    def update_ticker(self, tk: str, params: dict) -> dict:
        """Per-ticker калибровка: entry_days_before/exit_offset_days/min_div_yield_pct/
        stop_loss_pct/entry_notional_rub поверх глобала. Переживает рестарт. Значение None
        или отсутствие ключа снимает оверрайд (возврат к глобалу). Пересоздаёт движок,
        сохраняя открытую позицию (событийная стратегия — параметры влияют на будущие входы)."""
        if tk not in ST8_TICKERS:
            raise ValueError(f"неизвестный тикер {tk}")
        ranges = {"entry_days_before": (1, 30, int), "exit_offset_days": (0, 5, int),
                  "min_div_yield_pct": (0, 20, float), "stop_loss_pct": (0, 30, float),
                  "entry_notional_rub": (1000, 100_000_000, float)}
        ov = dict(self.ticker_overrides.get(tk) or {})
        for key, (lo, hi, cast) in ranges.items():
            if key not in params:
                continue
            if params[key] is None:
                ov.pop(key, None)               # снять оверрайд → глобал
                continue
            v = cast(params[key])
            if not (lo <= v <= hi):
                raise ValueError(f"{key}: вне [{lo}, {hi}]")
            ov[key] = v
        if ov:
            self.ticker_overrides[tk] = ov
        else:
            self.ticker_overrides.pop(tk, None)
        self._rebuild_engine(tk)
        eff = {k: self._p(tk, k) for k in self.PER_TICKER_KEYS}
        self.log_event("info", f"{tk}: параметры обновлены (вход −{eff['entry_days_before']}д, "
                               f"выход ex−{eff['exit_offset_days']}, миндох {eff['min_div_yield_pct']}%, "
                               f"стоп {eff['stop_loss_pct']}%, нотионал {int(eff['entry_notional_rub'])})")
        self.save_session()
        return {"ticker": tk, "overrides": self.ticker_overrides.get(tk, {}), "effective": eff}

    def _exec(self):
        """Ленивый St8Executor под текущий режим/счёт (смена любого — пересоздание)."""
        from .executor import St8Executor
        real = self.cfg.mode == "tbank_real"
        paper = self.cfg.mode == "paper"
        if (self._executor is None or self._executor.account_id != self.cfg.account_id
                or self._executor.real != real or self._executor.paper != paper):
            self._executor = St8Executor(
                self.cfg.account_id, paper=paper, real=real, armed_cb=self._real_armed,
                audit_cb=lambda a: self.log_event("order",
                    f"{a['op']} {a['direction']} {a['lots']}лот → {a.get('status')}"))
        return self._executor

    # ---------- боевой контур: взвод реальной торговли (канон st5) ----------
    def arm_real(self, armed: bool) -> None:
        """Двойной включатель. Взвод НЕ персистится (сбрасывается рестартом/сменой режима)."""
        self.state["real_trading_armed"] = bool(armed)
        self.log_event("warn" if armed else "info",
                       "🔴 ST8: реальная торговля ВЗВЕДЕНА" if armed else "ST8: взвод снят")

    def _real_armed(self) -> bool:
        """armed_cb исполнителя: взвод + cooldown 600с после старта live (защита от
        автоордеров на всплеске сразу после рестарта)."""
        if not self.state.get("real_trading_armed"):
            return False
        started = self.state.get("session_started") or 0
        return (time.time() - started) >= 600

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
        if not self._trading_days:
            return []   # торговый календарь ещё не загружен — считать нельзя, НЕ кэшируем
        try:
            d = _iss(f"{ISS}/securities/{tk}/dividends.json?iss.meta=off")
            dv = d.get("dividends", d)
            ci = {c: i for i, c in enumerate(dv["columns"])}
            today = date.today().isoformat()
            out = []
            for r in dv["data"]:
                rc = r[ci["registryclosedate"]]; val = r[ci["value"]]
                if not rc or not val:
                    continue
                ex = self._prev_trading_day(rc)
                if ex is None:
                    continue
                # дивдоходность: прошедшее событие — close на день гэпа; БУДУЩЕЕ — текущая
                # цена (close на будущую дату не существует, dy=0 отсекал бы событие фильтром)
                if ex <= today:
                    px = self._price_on(tk, ex)
                else:
                    px = (self.market.get(tk) or {}).get("last") or self._recent_close(tk)
                dy = (val / px * 100) if px else 0.0
                out.append((ex, float(val), round(dy, 2)))
            # пустоту НЕ кэшируем: могла быть гонка/сбой загрузки торговых дней (баг 09.07 —
            # первый tick после рестарта при упавшем ISS кэшировал [] для всех навсегда)
            if out:
                self._div_cache[tk] = out
            return out
        except Exception as e:  # noqa: BLE001
            self.log_event("warn", f"{tk}: дивиденды не получены: {str(e)[:80]}")
            return []

    def _load_trading_days(self, since: str) -> None:
        """Календарь торговых дней: история MOEX (IMOEX) + ПРОЕКЦИЯ будущего рабочими
        днями (пн–пт) на 200 дней вперёд. Без проекции будущие ex-даты не попадали в
        календарь: лонг не мог войти НИКОГДА (день ex−10 не совпадал с today), а
        _prev_trading_day для будущей регдаты возвращал вчерашний день — ложный ex и
        преждевременный шорт (критический баг, ревизия 11.07). Проекция самоисцеляется:
        при ежедневной перезагрузке прошедший день либо подтверждён историей, либо исчез.
        Риск незапланированного праздника: расчётный день сдвинется на 1, вход в закрытый
        рынок просто не исполнится (уйдёт в missed)."""
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
            if not days:
                return
            cur = date.fromisoformat(days[-1]) + timedelta(days=1)
            horizon = date.today() + timedelta(days=200)
            while cur <= horizon:
                if cur.weekday() < 5:
                    days.append(cur.isoformat())
                cur += timedelta(days=1)
            self._trading_days = days
            self._tdays_date = date.today().isoformat()
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

    def _recent_close(self, tk: str) -> float | None:
        """Последний close за ~10 дней (текущая цена для дивдоходности будущих событий)."""
        try:
            frm = (date.today() - timedelta(days=10)).isoformat()
            r = _iss(f"{ISS}/history/engines/stock/markets/shares/securities/{tk}.json"
                     f"?iss.meta=off&from={frm}&history.columns=TRADEDATE,CLOSE")
            data = [x for x in r["history"]["data"] if x and x[1]]
            return float(data[-1][1]) if data else None
        except Exception:  # noqa: BLE001
            return None

    def _lot(self, tk: str) -> int:
        """Лотность с кэшем; сбой ISS НЕ кэшируем (дефолт 1 только на этот вызов)."""
        if tk not in self._lot_cache:
            v = _iss_lot_size(tk)
            if v is None:
                return 1
            self._lot_cache[tk] = v
        return self._lot_cache[tk]

    # ---------- ФЬЮЧЕРСНОЕ ИСПОЛНЕНИЕ (плечо ~3× через ГО) ----------
    def near_future(self, tk: str, min_expiry: str) -> str | None:
        """Ближайший квартальник на актив tk с экспирацией ПОСЛЕ min_expiry (живёт всё окно
        сделки). None = фьючерсов нет (fallback на акцию). Кэш на день."""
        today = date.today().isoformat()
        c = self._fut_cache.get(tk)
        if c and c[2] == today and (c[0] is None or c[1] > min_expiry):
            return c[0]
        futs = _iss_futures_for_asset(tk)
        pick = next(((sec, exp) for sec, exp in futs if exp > min_expiry), None)
        self._fut_cache[tk] = (pick[0] if pick else None, pick[1] if pick else "", today)
        return pick[0] if pick else None

    def _pv(self, secid: str) -> float | None:
        """Пункт-стоимость фьючерса ₽ (STEPPRICE/MINSTEP). None при сбое —
        прежний дефолт 1.0 искажал сайзинг/P&L (исполняемся тогда акцией)."""
        if secid not in self._pv_cache:
            try:
                from ..st6.data import point_value
                self._pv_cache[secid] = float(point_value(secid))
            except Exception:  # noqa: BLE001
                return None
        return self._pv_cache[secid]

    def _instrument_for(self, tk: str, ex_date: str, hold_after: int = 7):
        """Инструмент исполнения события: (secid_фьюча|None, котировки, pv, lot_size).
        use_futures и фьюч есть → фьючерс (котировки live FORTS, P&L через pv);
        иначе акция (котировки из self.market, lot_size акции)."""
        s = self.cfg.strategy
        if getattr(s, "use_futures", False):
            # экспирация должна быть позже конца окна (ex + hold_after дней с запасом)
            min_exp = (date.fromisoformat(ex_date) + timedelta(days=hold_after)).isoformat()
            sec = self.near_future(tk, min_exp)
            pv = self._pv(sec) if sec else None
            if sec and pv:
                # котировка строго ЖИВАЯ: прежний кэш fut_market отдавал вход по цене
                # многодневной давности; кэш остаётся только для отображения на дашборде
                q = _iss_fut_quote(sec)
                if q:
                    self.fut_market[tk] = q
                    return sec, q, pv, 1
        return None, self.market.get(tk, {}), 1.0, None   # акция (lot_size возьмёт engine)

    def free_cash_rub(self) -> float:
        """Свободный кэш: sandbox/real — деньги счёта (API); paper — капитал − нотионалы."""
        if self.cfg.account_id and self.cfg.mode in ("tbank_sandbox", "tbank_real"):
            try:
                if self.cfg.mode == "tbank_real":
                    from ..st4 import tbank_live as live
                    for m in live.positions(self.cfg.account_id).get("money", []):
                        if m.get("currency") == "rub":
                            from ..st4 import tbank_sandbox as sb
                            return float(sb._q_to_float(m))
                else:
                    from ..st4 import tbank_sandbox as sb
                    return float(sb.free_money_rub(self.cfg.account_id))
            except Exception:  # noqa: BLE001
                pass
        base = self.capital_rub or 1_000_000.0
        used = sum(abs(e.position.stock_entry * e.position.lots * e.lot_size)
                   for e in self.engines.values() if e.position is not None)
        return max(0.0, base - used)

    def _position_lots(self, px: float, unit_value: float, tk: str | None = None) -> int:
        """Лоты входа из целевого нотионала: manual ₽ или % свободного кэша.
        В tbank_real нотионал дополнительно режется потолком real_max_notional_rub.
        tk задан → per-ticker entry_notional_rub (калибровка размера под эмитент)."""
        s = self.cfg.strategy
        target = 0.0
        if s.sizing_mode == "cash_pct" and s.entry_cash_pct > 0:
            target = self.free_cash_rub() * s.entry_cash_pct / 100.0
        if target <= 0:
            target = self._p(tk, "entry_notional_rub") if tk else s.entry_notional_rub
        cap = getattr(s, "real_max_notional_rub", 0.0)
        if self.cfg.mode == "tbank_real" and cap > 0:
            target = min(target, cap)      # боевой лимит объёма (пилот — малый размер)
        if target <= 0 or px <= 0 or unit_value <= 0:
            return max(1, s.quantity_lots)
        return max(1, int(target / (px * unit_value)))

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
        """Капитал счёта (sandbox или боевого) + якорь аудита. ЖУРНАЛ ВРЁТ — истина
        только кэш счёта (урок band/st4: журнальный P&L расходился с реальным вдвое)."""
        if self.cfg.mode not in ("tbank_sandbox", "tbank_real") or not self.cfg.account_id:
            return
        try:
            from ..st4 import tbank_sandbox as sb
            if self.cfg.mode == "tbank_real":
                from ..st4 import tbank_live as live
                pf = live.portfolio(self.cfg.account_id)
            else:
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
    @staticmethod
    def in_exec_window() -> bool:
        """Окно исполнения ПЛАНОВЫХ сделок: 17:00–18:45 МСК, у закрытия основной сессии.
        Бэктест считан по ценам ЗАКРЫТИЯ дня — вход/выход утренним тиком добавлял бы
        невалидированный внутридневной дрейф (особенно шорт в день гэпа: модель входит
        на close ex-дня). Стопы исполняются ЛЮБЫМ тиком. Окно 105 мин > часового опроса —
        хотя бы один тик гарантированно попадает."""
        msk = datetime.now(timezone.utc) + timedelta(hours=3)
        return (17, 0) <= (msk.hour, msk.minute) <= (18, 45)

    def _hedge_lots_for(self, stock_px: float, stock_lots: int, unit_value: float) -> int:
        """Сколько лотов IMOEXF шортить под нотионал позиции × hedge_ratio.
        Нотионал = px × lots × unit_value (пункт-стоимость фьюча или лотность акции)."""
        if not self.cfg.strategy.hedge_imoexf or not self.hedge_px:
            return 0
        notional = stock_px * stock_lots * unit_value * self.cfg.strategy.hedge_ratio
        hedge_notional = self.hedge_px * 10.0   # пункт-стоимость IMOEXF = 10₽
        return max(0, round(notional / hedge_notional)) if hedge_notional > 0 else 0

    def tick(self) -> dict:
        """Один daily-тик: скан дивидендов → котировки → для каждой бумаги проверить
        вход (ex−N) / выход (ex−1 или стоп) → исполнить. Возвращает сводку действий."""
        today = date.today().isoformat()
        acted = {"entered": [], "exited": [], "missed": 0}
        # календарь перезагружаем раз в день (проекция будущего в списке всегда «свежее»
        # today — прежнее условие `последний < today` с проекцией не сработало бы никогда)
        if not self._trading_days or self._tdays_date != today:
            self._load_trading_days((date.today() - timedelta(days=400)).isoformat())
        self.scan_new_dividends()
        self._sleeping = self.sleeping_tickers()
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
        # дневной лимит убытка: реализованный минус за сегодня → HALT входов (выходы работают)
        loss_limit = self.cfg.strategy.daily_loss_limit_rub
        day_realized = sum(t.get("net_pnl_rub", 0) for t in self.trades
                           if t.get("exit_date") == today)
        entries_halted = loss_limit > 0 and day_realized <= -loss_limit
        if entries_halted:
            self.log_event("warn", f"дневной лимит убытка: {day_realized:+.0f}₽ — входы HALT")
        for tk in ST8_TICKERS:
            if not self.enabled.get(tk, False):
                continue
            eng = self._engine(tk)
            if self.cfg.mode in ("tbank_sandbox", "tbank_real"):
                eng.lot_size = self._lot(tk)   # актуализация (сбой ISS при создании давал 1)
            q = self.market.get(tk, {})
            stock_px = q.get("offer") or q.get("last")   # вход по ask (реализм)
            # ── ВЫХОД (приоритет: стоп, потом плановый; лонг и шорт) ──
            if eng.position is not None:
                is_short = eng.position.side == "short"
                fut = eng.position.instrument or None
                cq = (_iss_fut_quote(fut) or {}) if fut else q   # котировки исполнителя
                # лонг закрываем по bid (продаём), шорт выкупаем по offer (покупаем)
                close_px = ((cq.get("offer") if is_short else cq.get("bid"))
                            or cq.get("last"))
                if close_px is None:
                    continue   # нет котировки исполнителя — прежний fallback на цену входа
                    #            писал в журнал фиктивный «выход в ноль»; ждём следующий тик
                stop = eng.check_stop(close_px, self.hedge_px or eng.position.hedge_entry)
                out_day = (eng.short_exit_day(self._trading_days) if is_short
                           else eng.exit_day(self._trading_days))
                # фейлсейф от залипания: ex_date выпал из календаря → out_day None →
                # плановый выход не сработает; закрываем по календарной просрочке
                overdue = out_day is None and eng.overdue_exit(today)
                planned = (out_day is not None and today >= out_day) or overdue
                if overdue:
                    self.log_event("warn", f"{tk}: 🚨 фейлсейф-выход — ex_date {eng.position.ex_date} "
                                           f"вне календаря, закрываем во избежание залипания")
                # плановый выход — в окне у закрытия (модель close-to-close); стоп — любым тиком
                if stop or (planned and self.in_exec_window()):
                    if not market_open:
                        continue
                    # P&L хеджа теряется, если IMOEXF-котировки нет: close по hedge_entry даёт
                    # hedge_pnl=0 (фикция). ПЛАНОВЫЙ выход с активным хеджем без котировки —
                    # откладываем до следующего тика (журнал не должен врать). СТОП/фейлсейф
                    # закрываем всё равно (риск важнее точности), пометив недостоверность.
                    hedge_active = (not is_short) and eng.position.hedge_lots > 0
                    if hedge_active and self.hedge_px is None:
                        # откладываем ТОЛЬКО пока не достигли ex-date (есть запас времени).
                        # После ex-date держать сквозь гэп хуже недостоверного P&L хеджа —
                        # форсируем выход (аудит 15.07: иначе устойчивый сбой IMOEXF = вечное
                        # залипание планового лонга сквозь дивгэп).
                        before_ex = today < eng.position.ex_date
                        if planned and not stop and not overdue and before_ex:
                            self.log_event("warn", f"{tk}: плановый выход отложен — нет котировки "
                                                   f"IMOEXF (есть запас до ex {eng.position.ex_date})")
                            continue
                        self.log_event("warn", f"{tk}: 🚨 выход БЕЗ котировки IMOEXF (у ex-date/стоп) "
                                               f"— P&L хеджа недостоверен, сверь по счёту")
                    reason = "stop" if stop else "exit"
                    req_lots = eng.position.lots
                    req_hedge = eng.position.hedge_lots
                    if is_short:
                        rc = self._exec().close_short(tk, req_lots, close_px, fut_secid=fut)
                        s_closed = rc.get("stock_closed", req_lots)
                        h_closed = 0
                    else:
                        rc = self._exec().close(tk, req_lots, close_px, req_hedge,
                                                self.hedge_px or 0, fut_secid=fut)
                        s_closed = rc.get("stock_closed", req_lots)
                        h_closed = rc.get("hedge_closed", req_hedge)
                    # ЧАСТИЧНОЕ закрытие → НЕ фиксируем сделку, ведём остаток (иначе движок
                    # flat при висящих на счёте лотах = голая нога, грабли st5). Полное
                    # закрытие обеих ног → штатная фиксация в журнал.
                    if s_closed < req_lots or (req_hedge > 0 and h_closed < req_hedge):
                        eng.position.lots = max(0, req_lots - s_closed)
                        eng.position.hedge_lots = max(0, req_hedge - h_closed)
                        self.log_event("warn", f"{tk}: 🚨 частичный выход — закрыто акция {s_closed}/"
                                               f"{req_lots}, хедж {h_closed}/{req_hedge}; остаток ведём, "
                                               f"выход повторится следующим тиком")
                        if eng.position.lots == 0 and eng.position.hedge_lots == 0:
                            eng.position = None   # всё же закрылось до нуля — обнулить
                        self.save_session()
                        continue
                    tr = eng.close(today, close_px, self.hedge_px or eng.position.hedge_entry, reason)
                    self.trades.append(_trade_dict(tr))
                    acted["exited"].append(tk)
                    self.log_event("exit", f"{tk}: выход {eng_side_label(tr.side)} ({reason}) "
                                           f"net {tr.net_pnl_rub:+.0f}₽")
                continue
            # ── ШОРТ-ВХОД (день гэпа = ex; модель входит на ЗАКРЫТИИ ex-дня → окно 17:00+) ──
            sev = eng.short_entry_signal(today, events, self._trading_days)
            if sev is not None:
                if not self.in_exec_window():
                    continue   # план-вход у закрытия; не missed — день ещё не кончился
                if entries_halted:
                    self.log_missed(tk, today, sev.ex_date, "дневной лимит убытка")
                    acted["missed"] += 1
                    continue
            if sev is not None and self.cfg.trading_enabled and market_open:
                fut, iq, pv, _lot = self._instrument_for(tk, sev.ex_date)
                uval = pv if fut else float(eng.lot_size)
                short_px = iq.get("bid") or iq.get("last")   # шорт продаёт по bid (реализм)
                sp = iq.get("spread_pct")
                if sp is None and iq.get("bid") and iq.get("offer"):
                    sp = (iq["offer"] - iq["bid"]) / iq["bid"] * 100
                max_sp = self.cfg.strategy.max_spread_pct
                if short_px and not (max_sp > 0 and sp is not None and sp > max_sp):
                    lots = self._position_lots(short_px, uval, tk)
                    try:
                        r = self._exec().open_short(tk, lots, short_px, fut_secid=fut)
                        eng.open(today, sev, short_px, 0.0, 0, side="short",
                                 instrument=fut or "", unit_value=uval,
                                 lots=r["stock_filled"])
                        acted["entered"].append(tk + ":short")
                        self.log_event("position", f"{tk}: ШОРТ {r['stock_filled']}лот"
                                                   f"{' '+fut if fut else ''} @ {short_px} "
                                                   f"(сдувание после отсечки {sev.ex_date})")
                        continue
                    except Exception as e:  # noqa: BLE001
                        self.log_missed(tk, today, sev.ex_date, f"шорт брокер: {str(e)[:80]}")
                        acted["missed"] += 1
            # ── ВХОД (день входа = ex−N; модель по close → окно 17:00+) ──
            ev = eng.entry_signal(today, events, self._trading_days)
            if ev is None:
                continue
            if not self.in_exec_window():
                continue   # план-вход у закрытия; не missed — день ещё не кончился
            if entries_halted:
                self.log_missed(tk, today, ev.ex_date, "дневной лимит убытка")
                acted["missed"] += 1
                continue
            if not (self.cfg.trading_enabled and market_open and stock_px):
                self.log_missed(tk, today, ev.ex_date,
                                "торговля выкл" if not self.cfg.trading_enabled else "рынок закрыт/нет цены")
                acted["missed"] += 1
                continue
            fut, iq, pv, _lot = self._instrument_for(tk, ev.ex_date)
            uval = pv if fut else float(eng.lot_size)
            entry_px = iq.get("offer") or iq.get("last") or stock_px   # вход по ask исполнителя
            # фильтр ликвидности — спред ИНСТРУМЕНТА ИСПОЛНЕНИЯ (раньше при фьючерсном
            # исполнении проверялся спред акции): широкий спред съедает edge
            max_sp = self.cfg.strategy.max_spread_pct
            sp = iq.get("spread_pct")
            if sp is None and iq.get("bid") and iq.get("offer"):
                sp = (iq["offer"] - iq["bid"]) / iq["bid"] * 100
            if max_sp > 0 and sp is not None and sp > max_sp:
                self.log_missed(tk, today, ev.ex_date, f"спред {sp:.2f}% > {max_sp}% (дорогое исполнение)")
                acted["missed"] += 1
                continue
            lots = self._position_lots(entry_px, uval, tk)
            # ГЕЙТ ГОЛОЙ БЕТЫ: если хедж обязателен (hedge_imoexf), но котировки IMOEXF нет,
            # _hedge_lots_for вернёт 0 → лонг открылся бы БЕЗ хеджа (голая бета 10 дней, в 2022
            # −45%). Отказываем во входе (аудит 15.07): лучше пропустить, чем нехеджированно.
            if self.cfg.strategy.hedge_imoexf and not self.hedge_px:
                self.log_missed(tk, today, ev.ex_date, "нет котировки IMOEXF — хедж невозможен")
                acted["missed"] += 1
                continue
            hlots = self._hedge_lots_for(entry_px, lots, uval)
            try:
                r = self._exec().open(tk, lots, entry_px, hlots, self.hedge_px or 0,
                                      fut_secid=fut)
                eng.open(today, ev, entry_px, self.hedge_px or 0, r.get("hedge_filled", 0),
                         instrument=fut or "", unit_value=uval, lots=r["stock_filled"])
                acted["entered"].append(tk)
                self.log_event("position", f"{tk}: ВХОД {r['stock_filled']}лот"
                                           f"{' '+fut if fut else ''} @ {entry_px} "
                                           f"+ хедж {r.get('hedge_filled',0)} IMOEXF (отсечка {ev.ex_date})")
            except Exception as e:  # noqa: BLE001
                self.log_missed(tk, today, ev.ex_date, f"брокер: {str(e)[:80]}")
                acted["missed"] += 1
        self.refresh_capital()
        self.save_session()
        self.last_tick_ts = int(time.time() * 1000)
        return acted

    async def run_live(self) -> None:
        import asyncio
        self.state["live"] = True
        while self.state["live"]:
            try:
                # wait_for: зависший тик (DNS getaddrinfo не покрыт urllib-timeout —
                # инцидент 09.07: цикл замер на 3+ часа) НЕ убивает цикл; поток-зомби
                # доживёт сам, цикл продолжает следующим тиком
                await asyncio.wait_for(asyncio.to_thread(self.tick),
                                       timeout=max(120.0, self.cfg.poll_seconds * 0.9))
            except asyncio.TimeoutError:
                self.log_event("warn", "тик завис (таймаут) — пропущен, цикл жив")
            except Exception as e:  # noqa: BLE001
                self.log_event("warn", f"тик не удался: {str(e)[:100]}")
            await asyncio.sleep(self.cfg.poll_seconds)

    def start_live(self) -> None:
        import asyncio
        if self._task is not None and not self._task.done():
            return   # цикл реально жив (проверка task, НЕ флага)
        self.state["live"] = True
        self.state["live_intent"] = True
        self.state["session_started"] = time.time()   # точка отсчёта cooldown боевого взвода
        self._task = asyncio.create_task(self.run_live())
        self.log_event("info", f"ST8 live запущен ({self.cfg.mode}, дивидендный набег)")
        self.save_session()

    def stop_live(self) -> None:
        self.state["live"] = False
        self.state["live_intent"] = False
        self.log_event("info", "ST8 остановлен")
        self.save_session()

    def sleeping_tickers(self) -> list[str]:
        """Бумаги без дивидендов >365 дней (заморожены, напр. GMKN с 2023 — конфликт
        акционеров). Физически не могут торговаться в стратегии, пока не возобновят.
        Требует загруженных _trading_days (иначе пустой список — не пугаемся)."""
        if not self._trading_days:
            return []
        cutoff = (date.today() - timedelta(days=365)).isoformat()
        out = []
        for tk in ST8_TICKERS:
            if not self.enabled.get(tk, False):
                continue
            divs = self._fetch_divs(tk)
            if not divs or divs[-1][0] < cutoff:
                out.append(tk)
        return out

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
                # дни входа/выхода по торговому календарю (лонг + шорт-нога)
                entry_d = exit_d = short_exit_d = None
                if ex in self._trading_days:
                    ex_i = self._trading_days.index(ex)
                    if ex_i - s.entry_days_before >= 0:
                        entry_d = self._trading_days[ex_i - s.entry_days_before]
                    if ex_i - s.exit_offset_days >= 0:
                        exit_d = self._trading_days[ex_i - s.exit_offset_days]
                    if ex_i + s.short_hold_days < len(self._trading_days):
                        short_exit_d = self._trading_days[ex_i + s.short_hold_days]
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
                # цены входа/выхода (close на день входа/выхода — какой была бы сделка)
                entry_px = self._price_on(tk, entry_d) if entry_d and entry_d <= today else None
                exit_px = self._price_on(tk, exit_d) if exit_d and exit_d <= today else None
                run_pct = (round((exit_px - entry_px) / entry_px * 100, 2)
                           if entry_px and exit_px else None)   # набег до гэпа, %
                # шорт торгуется в июле (его лучший месяц), фильтр — только skip_months
                short_on = (s.short_enabled and int(ex[5:7]) not in (s.short_skip_months or []))
                rows.append({
                    "ticker": tk, "name": name, "ex_date": ex,
                    "entry_date": entry_d, "exit_date": exit_d,
                    "entry_px": entry_px, "exit_px": exit_px, "run_pct": run_pct,
                    "short_entry_date": ex if short_on else None,
                    "short_exit_date": short_exit_d if short_on else None,
                    "div": div, "div_yield_pct": dy,
                    "july": is_july, "status": status,
                })
        rows.sort(key=lambda r: (r["entry_date"] or r["ex_date"]))
        return rows

    def price_series(self, ticker: str, ex_date: str, pad: int = 20) -> list[dict]:
        """Дневные close вокруг события (для мини-графика при наведении): от entry−pad до
        ex+5 дней. Возвращает [{date, close}]. Помечает вход/выход/ex через build_calendar."""
        try:
            frm = (datetime.fromisoformat(ex_date) - timedelta(days=45)).date().isoformat()
            to = (datetime.fromisoformat(ex_date) + timedelta(days=10)).date().isoformat()
            r = _iss(f"{ISS}/history/engines/stock/markets/shares/securities/{ticker}.json"
                     f"?iss.meta=off&from={frm}&till={to}&history.columns=TRADEDATE,CLOSE")
            out = []
            for row in r["history"]["data"]:
                if row and row[0] and row[1]:
                    out.append({"date": row[0], "close": float(row[1])})
            return out
        except Exception:  # noqa: BLE001
            return []

    # ---------- снимок ----------
    def snapshot(self) -> dict:
        net = sum(t.get("net_pnl_rub", 0) for t in self.trades)
        return {
            "strategy": "st8", "live": self.state["live"], "mode": self.cfg.mode,
            "account_id": self.cfg.account_id or None,
            "trading_enabled": self.cfg.trading_enabled,
            "real_trading_armed": bool(self.state.get("real_trading_armed")),  # боевой взвод
            "tickers_enabled": [tk for tk, on in self.enabled.items() if on],
            "open_positions": [
                {"ticker": tk, "entry": e.position.entry_date, "ex": e.position.ex_date,
                 "lots": e.position.lots, "side": e.position.side,
                 "instrument": e.position.instrument or None}
                for tk, e in self.engines.items() if e.position is not None
            ],
            "net_pnl_rub": round(net),
            "trades_count": len(self.trades),
            "capital_rub": round(self.capital_rub) or None,
            "execution_gap_rub": self.execution_gap(),      # аудит журнал↔счёт (кэш-истина)
            "new_dividends": self.new_dividends[-15:],        # свежеобъявленные (мониторинг)
            "hedge_px": self.hedge_px,
            "market_quotes": len(self.market),
            "last_tick_ts": self.last_tick_ts,
            "missed": self.missed[-15:],
            "sleeping": self._sleeping,          # без дивидендов >года (не торгуются)
            "trades_tail": self.trades[-20:],    # хвост журнала для страницы /st8
            "strategy_cfg": self.cfg.strategy.model_dump(),
            "ticker_overrides": self.ticker_overrides,
            "events": self.events[-20:],
        }

    def ledger(self, days_back: int = 30) -> dict:
        """Все действия по портфелю: транзакции доходов/расходов/комиссий + текущий баланс.
        sandbox — реальные операции счёта (GetSandboxOperations, кэш-истина);
        paper — синтез из журнала: на сделку строка P&L (gross) и строка комиссии."""
        rows = []
        if self.cfg.mode in ("tbank_sandbox", "tbank_real") and self.cfg.account_id:
            try:
                from ..st4 import tbank_sandbox as sb
                import datetime as _dtm
                now = _dtm.datetime.now(_dtm.timezone.utc)
                frm = (now - _dtm.timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
                to = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                if self.cfg.mode == "tbank_real":
                    from ..st4 import tbank_live as live
                    ops = live.operations(self.cfg.account_id, frm, to)
                else:
                    ops = sb._call("tinkoff.public.invest.api.contract.v1.SandboxService",
                                   "GetSandboxOperations",
                                   {"accountId": self.cfg.account_id, "from": frm, "to": to,
                                    "state": "OPERATION_STATE_EXECUTED"},
                                   token=sb._account_token(self.cfg.account_id)).get("operations", [])
                for o in ops:
                    amt = sb._q_to_float(o.get("payment"))
                    rows.append({"date": str(o.get("date", ""))[:16].replace("T", " "),
                                 "kind": o.get("operationType", "").replace("OPERATION_TYPE_", "").lower(),
                                 "label": o.get("instrumentUid", "") and (o.get("figi") or "инструмент") or "счёт",
                                 "amount": round(amt, 2)})
                rows.sort(key=lambda r: r["date"], reverse=True)
            except Exception as e:  # noqa: BLE001
                rows.append({"date": "", "kind": "error", "label": str(e)[:80], "amount": 0})
        else:
            # paper: старт капитала + по сделке (gross P&L и комиссия отдельными строками)
            for t in self.trades:
                gross = (t.get("stock_pnl_rub", 0) or 0) + (t.get("hedge_pnl_rub", 0) or 0)
                lbl = f"{t.get('ticker')} {eng_side_label(t.get('side','long'))} {t.get('entry_date')}→{t.get('exit_date')}"
                rows.append({"date": t.get("exit_date", ""), "kind": "trade_pnl",
                             "label": lbl, "amount": round(gross, 2)})
                if t.get("fees_rub"):
                    rows.append({"date": t.get("exit_date", ""), "kind": "fee",
                                 "label": f"комиссия {t.get('ticker')}", "amount": -round(t["fees_rub"], 2)})
            rows.sort(key=lambda r: r["date"], reverse=True)
        net = sum(t.get("net_pnl_rub", 0) for t in self.trades)
        return {"rows": rows[:200],
                "balance_rub": round(self.capital_rub) if self.capital_rub else None,
                "free_cash_rub": round(self.free_cash_rub()),
                "journal_net_rub": round(net),
                "fees_total_rub": round(sum(t.get("fees_rub", 0) for t in self.trades), 2)}

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

    def flat_all(self) -> dict:
        """Паник-закрытие ВСЕХ открытых позиций ST8 по рынку (штатно: executor + журнал).
        Лонг → продать акцию + откупить хедж IMOEXF (close); шорт → выкупить (close_short).
        Воспроизводит выход из tick, но без гейтов плана/окна — закрываем немедленно.
        Best-effort по котировке исполнителя; при её отсутствии ось пропускаем (не фиктивим)."""
        closed, skipped = [], []
        today = date.today().isoformat()
        for tk in ST8_TICKERS:
            eng = self.engines.get(tk)
            if eng is None or eng.position is None:
                continue
            p = eng.position
            is_short = p.side == "short"
            fut = p.instrument or None
            cq = (_iss_fut_quote(fut) or {}) if fut else self.market.get(tk, {})
            # лонг продаём по bid, шорт выкупаем по offer
            close_px = ((cq.get("offer") if is_short else cq.get("bid")) or cq.get("last"))
            if close_px is None:
                skipped.append({"ticker": tk, "reason": "нет котировки исполнителя"})
                self.log_event("warn", f"{tk}: flat-all пропущен — нет котировки")
                continue
            try:
                req_lots, req_hedge = p.lots, p.hedge_lots
                if is_short:
                    rc = self._exec().close_short(tk, req_lots, close_px, fut_secid=fut)
                    s_closed, h_closed = rc.get("stock_closed", req_lots), 0
                else:
                    rc = self._exec().close(tk, req_lots, close_px, req_hedge,
                                            self.hedge_px or 0, fut_secid=fut)
                    s_closed = rc.get("stock_closed", req_lots)
                    h_closed = rc.get("hedge_closed", req_hedge)
                # частичное закрытие → ведём остаток (не фиктивим полное закрытие)
                if s_closed < req_lots or (req_hedge > 0 and h_closed < req_hedge):
                    p.lots = max(0, req_lots - s_closed)
                    p.hedge_lots = max(0, req_hedge - h_closed)
                    if p.lots == 0 and p.hedge_lots == 0:
                        eng.position = None
                    skipped.append({"ticker": tk, "reason": f"частично: акция {s_closed}/{req_lots}, "
                                                            f"хедж {h_closed}/{req_hedge}"})
                    self.log_event("warn", f"{tk}: 🚨 flat-all частично — остаток ведём")
                    continue
                tr = eng.close(today, close_px, self.hedge_px or p.hedge_entry, "flat_all")
                self.trades.append(_trade_dict(tr))
                closed.append({"ticker": tk, "side": tr.side, "lots": tr.lots,
                               "net_pnl_rub": tr.net_pnl_rub})
                self.log_event("exit", f"{tk}: flat-all {eng_side_label(tr.side)} "
                                       f"net {tr.net_pnl_rub:+.0f}₽")
            except Exception as e:  # noqa: BLE001
                skipped.append({"ticker": tk, "reason": str(e)[:80]})
                self.log_event("warn", f"{tk}: flat-all ошибка — {str(e)[:80]}")
        self.save_session()
        return {"ok": True, "closed": closed, "skipped": skipped}

    def save_session(self) -> None:
        try:
            data = {"config": self.cfg.model_dump(), "trades": self.trades,
                    "enabled": self.enabled, "state": self.state,
                    "ticker_overrides": self.ticker_overrides,
                    "exec_anchor": self.exec_anchor, "new_dividends": self.new_dividends[-100:],
                    "div_seen": self._div_seen, "missed": self.missed[-100:],
                    # позиции ПЕРСИСТЯТСЯ (ревизия 11.07: рестарт с открытой позицией
                    # терял её — лоты оставались на счёте бесхозными, грабли st5)
                    "positions": {tk: asdict(e.position)
                                  for tk, e in self.engines.items() if e.position is not None}}
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
        # live — рантайм-факт, из файла не восстанавливаем (см. st9: фиктивный live без цикла)
        st = d.get("state") or {}
        st["live"] = False
        st["real_trading_armed"] = False   # взвод НЕ переживает рестарт (safe-by-default)
        self.state.update(st)
        en = d.get("enabled") or {}
        self.enabled = {tk: bool(en.get(tk, tk in ST8_CORE or tk in ST8_EXTENDED)) for tk in ST8_TICKERS}
        # per-ticker оверрайды (только известные тикеры и валидные ключи)
        self.ticker_overrides = {
            tk: {k: v for k, v in (ov or {}).items() if k in self.PER_TICKER_KEYS}
            for tk, ov in (d.get("ticker_overrides") or {}).items() if tk in ST8_TICKERS}
        self.ticker_overrides = {tk: ov for tk, ov in self.ticker_overrides.items() if ov}
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
        # восстановление открытых позиций (unit_value в позиции — P&L/стоп корректны,
        # даже если лотность при загрузке недоступна)
        from .engine import St8Position
        for tk, pd in (d.get("positions") or {}).items():
            if tk not in ST8_TICKERS:
                continue
            try:
                self._engine(tk).position = St8Position(**pd)
            except Exception:  # noqa: BLE001
                self.log_event("warn", f"{tk}: позиция из session не восстановлена")
        return True
