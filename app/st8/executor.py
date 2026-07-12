"""St8Executor — исполнение «дивидендного набега»: покупка/продажа спот-акции TQBR +
одновременный шорт фьючерса IMOEXF (хедж беты). Paper / sandbox / БОЕВОЙ (tbank_real).

Уроки прошлых инцидентов заложены:
- сверка executed_lots (частичный филл → unwind реально налитого, а не «поверили запросу»);
- аварийное/крупное закрытие МЕЛКИМИ ордерами (песочница отклоняет крупный по ёмкости 30034);
- order_id: sandbox строго UUID (иначе HTTP 400 30028), боевой — идемпотентный sha256
  с дискриминатором операции (ретрай при сетевом обрыве не задвоит ордер, канон st5);
- атомарность пары акция+хедж: если хедж не встал — откат акции (не оставлять голую бету).
paper — виртуальные филлы по переданным ценам (last/offer/bid), без реальных ордеров.

⚠️ БОЕВОЙ контур (real=True): post_order тратит РЕАЛЬНЫЕ деньги. Гейт НА УРОВНЕ ОРДЕРА,
для ВСЕХ ордеров (вход/выход/unwind/хедж): real==True требует armed_cb() ==True
(real_trading_armed + cooldown 600с в сервисе). trading_enabled здесь НЕ проверяется —
это гейт только ВХОДА (уровень tick), выходы от него не зависят (иначе позиция залипнет).
Плюс pre-trade sanity: |market − ref| / ref > max_price_dev_pct → отказ (канон st5).
"""
from __future__ import annotations

import hashlib
import time
import uuid

from ..st4 import tbank_sandbox as _sb
from ..st4 import tbank_live as _live


class St8ExecError(Exception):
    pass


class St8Executor:
    def __init__(self, account_id: str, paper: bool = True, real: bool = False,
                 armed_cb=None, audit_cb=None, max_price_dev_pct: float = 0.05):
        self.account_id = account_id
        self.paper = paper
        self.real = real                   # боевой контур (реальные деньги)
        self.armed_cb = armed_cb           # () -> bool, взвод реальной торговли
        self.audit_cb = audit_cb           # callback(dict) — аудит-лог каждого ордера
        self.max_price_dev_pct = max_price_dev_pct
        self._seq = 0                      # счётчик для идемпотентных боевых order_id
        self._share_cache: dict[str, dict] = {}
        self._hedge_uid: str | None = None

    # ---------- резолв инструментов ----------
    def _share(self, ticker: str) -> dict:
        if self.paper:
            return {"uid": "paper_" + ticker, "lot": 1}   # paper: реальный резолв не нужен
        if ticker not in self._share_cache:
            self._share_cache[ticker] = _sb.find_share(ticker)
        return self._share_cache[ticker]

    def _instr_uid(self, ticker: str, fut_secid: str | None) -> str:
        """UID исполняемого инструмента: фьючерс (если задан) или акция."""
        if fut_secid:
            if self.paper:
                return "paper_" + fut_secid
            if fut_secid not in self._share_cache:
                self._share_cache[fut_secid] = _sb.find_future(fut_secid)
            return self._share_cache[fut_secid]["uid"]
        return self._share(ticker)["uid"]

    def _hedge(self) -> str:
        if self.paper:
            return "paper_IMOEXF"
        if self._hedge_uid is None:
            self._hedge_uid = _sb.find_future("IMOEXF")["uid"]
        return self._hedge_uid

    def share_lot(self, ticker: str) -> int:
        """Лотность акции (акций в 1 лоте): NLMK=10, CHMF=1, SBER=1 и т.д."""
        return int(self._share(ticker).get("lot", 1))

    # ---------- один ордер с защитами ----------
    def _order(self, uid: str, lots: int, direction: str, op: str, ref_px: float) -> dict:
        """Один ордер (BUY|SELL) с аудитом. paper → виртуальный филл по ref_px;
        sandbox → SandboxService; real → БОЕВОЙ OrdersService (гейт armed + sanity цены)."""
        full_dir = f"ORDER_DIRECTION_{direction}"
        if self.paper:
            audit = {"account": self.account_id, "uid": uid, "lots": lots,
                     "direction": direction, "op": op, "ref_px": ref_px,
                     "status": "paper_fill", "executed_lots": lots}
            if self.audit_cb:
                self.audit_cb(audit)
            return {"executionReportStatus": "FILL", "lotsExecuted": lots, "paper": True}
        if self.real:
            # гейт реальной торговли — на КАЖДЫЙ ордер (вход/выход/unwind/хедж)
            if self.armed_cb is None or not self.armed_cb():
                raise St8ExecError("реальная торговля не взведена (armed_cb) — ордер заблокирован")
            # pre-trade sanity: рыночная цена не должна аномально расходиться с ref
            try:
                mkt = _sb.last_price(uid)
                if mkt > 0 and ref_px > 0 and abs(mkt - ref_px) / ref_px > self.max_price_dev_pct:
                    raise St8ExecError(f"аномальная цена {uid}: market={mkt} ref={ref_px} "
                                       f"(>{self.max_price_dev_pct*100:.0f}%)")
            except St8ExecError:
                raise
            except Exception:  # noqa: BLE001  last_price недоступен — market исполнится и так
                pass
            # идемпотентный orderId с дискриминатором операции (ретрай не задвоит ордер)
            self._seq += 1
            raw = f"{self.account_id}|{uid}|{int(lots)}|{direction}|{op}|{self._seq}|{int(time.time())}"
            oid = hashlib.sha256(raw.encode()).hexdigest()[:32]
        else:
            oid = str(uuid.uuid4())        # sandbox требует строго UUID (400 30028)
        audit = {"account": self.account_id, "uid": uid, "lots": lots,
                 "direction": direction, "op": op, "order_id": oid, "ref_px": ref_px,
                 "real": self.real}
        try:
            api = _live if self.real else _sb
            resp = api.post_order(self.account_id, uid, lots, full_dir, oid)
            audit["status"] = "ok"
            audit["executed_lots"] = resp.get("lotsExecuted") or resp.get("executedLots")
        except Exception as e:  # noqa: BLE001
            audit["status"] = "error"
            audit["error"] = str(e)
            if self.audit_cb:
                self.audit_cb(audit)
            raise
        if self.audit_cb:
            self.audit_cb(audit)
        return resp

    @staticmethod
    def _filled(resp: dict, requested: int) -> int:
        """Фактически исполненные лоты. Нет поля → полный филл (совместимость)."""
        v = resp.get("lotsExecuted")
        if v is None:
            v = resp.get("executedLots")
        if v is None:
            return requested
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return requested

    def _close_small(self, uid: str, lots: int, direction: str, op: str, ref_px: float) -> None:
        """Закрыть позицию МЕЛКИМИ ордерами по 1 лоту (крупный отклоняется ёмкостью 30034)."""
        for _ in range(lots):
            try:
                self._order(uid, 1, direction, op, ref_px)
            except Exception:  # noqa: BLE001
                break

    # ---------- вход: акция long + хедж-шорт IMOEXF ----------
    def open(self, ticker: str, stock_lots: int, stock_px: float,
             hedge_lots: int, hedge_px: float, fut_secid: str | None = None) -> dict:
        """Купить инструмент (акцию или её фьючерс fut_secid) + шорт IMOEXF. Атомарно:
        хедж не встал — откат (не оставлять голую бету). {ok, stock_filled, hedge_filled}."""
        s_uid = self._instr_uid(ticker, fut_secid)
        r = self._order(s_uid, stock_lots, "BUY", "entry", stock_px)
        got_stock = self._filled(r, stock_lots)
        if got_stock == 0:
            raise St8ExecError(f"{ticker}: акция не налилась (0 лотов)")
        if got_stock < stock_lots:
            # частичный филл акции — работаем с реально налитым (не откатываем, набег
            # хеджируем); хедж масштабируем к налитому, иначе перехедж
            if hedge_lots > 0:
                hedge_lots = max(1, round(hedge_lots * got_stock / stock_lots))
            stock_lots = got_stock
        hedge_filled = 0
        if hedge_lots > 0:
            h_uid = self._hedge()
            try:
                rh = self._order(h_uid, hedge_lots, "SELL", "entry_hedge", hedge_px)
                hedge_filled = self._filled(rh, hedge_lots)
            except Exception as e:  # noqa: BLE001
                # ХЕДЖ НЕ ВСТАЛ → откат акции (иначе голая бета — против сути стратегии)
                self._close_small(s_uid, got_stock, "SELL", "unwind", stock_px)
                raise St8ExecError(f"{ticker}: хедж не исполнен → акция откачена: {str(e)[:80]}")
        return {"ok": True, "stock_filled": stock_lots, "hedge_filled": hedge_filled}

    # ---------- шорт-нога: продать без покрытия / выкупить ----------
    def open_short(self, ticker: str, stock_lots: int, stock_px: float,
                   fut_secid: str | None = None) -> dict:
        """Шорт инструмента (пост-дивидендное сдувание): SELL на открытие. Фьючерс
        предпочтителен (нет платы за заём); акция = маржин-шорт sandbox."""
        s_uid = self._instr_uid(ticker, fut_secid)
        r = self._order(s_uid, stock_lots, "SELL", "short_entry", stock_px)
        got = self._filled(r, stock_lots)
        if got == 0:
            raise St8ExecError(f"{ticker}: шорт не налился (0 лотов)")
        return {"ok": True, "stock_filled": got}

    def close_short(self, ticker: str, stock_lots: int, stock_px: float,
                    fut_secid: str | None = None) -> dict:
        """Выкуп шорта: BUY мелкими ордерами (ёмкость)."""
        s_uid = self._instr_uid(ticker, fut_secid)
        self._close_small(s_uid, stock_lots, "BUY", "short_exit", stock_px)
        return {"ok": True}

    # ---------- выход: продать акцию + откупить хедж ----------
    def close(self, ticker: str, stock_lots: int, stock_px: float,
              hedge_lots: int, hedge_px: float, fut_secid: str | None = None) -> dict:
        """Продать инструмент + откупить хедж-шорт. Мелкими ордерами (ёмкость). Best-effort:
        обе ноги закрываем независимо, ошибки логируются (выход не гейтить)."""
        s_uid = self._instr_uid(ticker, fut_secid)
        self._close_small(s_uid, stock_lots, "SELL", "exit", stock_px)
        if hedge_lots > 0:
            h_uid = self._hedge()
            self._close_small(h_uid, hedge_lots, "BUY", "exit_hedge", hedge_px)
        return {"ok": True}
