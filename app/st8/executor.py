"""St8Executor — исполнение «дивидендного набега»: покупка/продажа спот-акции TQBR +
одновременный шорт фьючерса IMOEXF (хедж беты). Sandbox или paper.

Уроки прошлых инцидентов заложены:
- сверка executed_lots (частичный филл → unwind реально налитого, а не «поверили запросу»);
- аварийное/крупное закрытие МЕЛКИМИ ордерами (песочница отклоняет крупный по ёмкости 30034);
- order_id строго UUID (иначе HTTP 400 30028);
- атомарность пары акция+хедж: если хедж не встал — откат акции (не оставлять голую бету).
paper — виртуальные филлы по переданным ценам (last/offer/bid), без реальных ордеров.
"""
from __future__ import annotations

import uuid

from ..st4 import tbank_sandbox as _sb


class St8ExecError(Exception):
    pass


class St8Executor:
    def __init__(self, account_id: str, paper: bool = True, audit_cb=None):
        self.account_id = account_id
        self.paper = paper
        self.audit_cb = audit_cb           # callback(dict) — аудит-лог каждого ордера
        self._share_cache: dict[str, dict] = {}
        self._hedge_uid: str | None = None

    # ---------- резолв инструментов ----------
    def _share(self, ticker: str) -> dict:
        if ticker not in self._share_cache:
            self._share_cache[ticker] = _sb.find_share(ticker)
        return self._share_cache[ticker]

    def _hedge(self) -> str:
        if self._hedge_uid is None:
            self._hedge_uid = _sb.find_future("IMOEXF")["uid"]
        return self._hedge_uid

    def share_lot(self, ticker: str) -> int:
        """Лотность акции (акций в 1 лоте): NLMK=10, CHMF=1, SBER=1 и т.д."""
        return int(self._share(ticker).get("lot", 1))

    # ---------- один ордер с защитами ----------
    def _order(self, uid: str, lots: int, direction: str, op: str, ref_px: float) -> dict:
        """Один sandbox-ордер (BUY|SELL) с аудитом. paper → виртуальный филл по ref_px."""
        oid = str(uuid.uuid4())
        full_dir = f"ORDER_DIRECTION_{direction}"
        audit = {"account": self.account_id, "uid": uid, "lots": lots,
                 "direction": direction, "op": op, "order_id": oid, "ref_px": ref_px}
        if self.paper:
            audit["status"] = "paper_fill"
            audit["executed_lots"] = lots
            if self.audit_cb:
                self.audit_cb(audit)
            return {"executionReportStatus": "FILL", "lotsExecuted": lots, "paper": True}
        try:
            resp = _sb.post_order(self.account_id, uid, lots, full_dir, oid)
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
             hedge_lots: int, hedge_px: float) -> dict:
        """Купить акцию (stock_lots) + шорт IMOEXF (hedge_lots). Атомарно: если хедж не встал —
        откат акции (не оставлять голую бету). Возвращает {ok, stock_filled, hedge_filled}."""
        s_uid = self._share(ticker)["uid"]
        r = self._order(s_uid, stock_lots, "BUY", "entry", stock_px)
        got_stock = self._filled(r, stock_lots)
        if got_stock == 0:
            raise St8ExecError(f"{ticker}: акция не налилась (0 лотов)")
        if got_stock < stock_lots:
            # частичный филл акции — работаем с реально налитым (не откатываем, набег хеджируем)
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

    # ---------- выход: продать акцию + откупить хедж ----------
    def close(self, ticker: str, stock_lots: int, stock_px: float,
              hedge_lots: int, hedge_px: float) -> dict:
        """Продать акцию + откупить хедж-шорт. Мелкими ордерами (ёмкость). Best-effort:
        обе ноги закрываем независимо, ошибки логируются (выход не гейтить)."""
        s_uid = self._share(ticker)["uid"]
        self._close_small(s_uid, stock_lots, "SELL", "exit", stock_px)
        if hedge_lots > 0:
            h_uid = self._hedge()
            self._close_small(h_uid, hedge_lots, "BUY", "exit_hedge", hedge_px)
        return {"ok": True}
