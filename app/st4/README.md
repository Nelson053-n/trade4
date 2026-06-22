# st4 — спред-арбитраж SBRF / SBPR (FORTS)

Статистический арбитраж спреда фьючерсов на обыкновенные (**SBRF**, серии `SR*`) и
привилегированные (**SBPR**, серии `SP*`) акции Сбербанка. Реализация ТЗ из папки `st4/`
адаптирована под Python-стек проекта (вместо C#/.NET) и встроена как вкладка **st4** в
общую панель `dashboard.html` рядом с st1/st2/st3. **Phase 1 — paper, без реальных ордеров.**

## Логика (по разделам ТЗ)

- **Спред** = `Close(SBPR) − Close(SBRF)` по синхронному 10-мин бару (§7, `indicators.SpreadBuilder`).
- **Индикатор** — Bollinger Bands(`SMA=200`, `2σ`), Population-σ, no repaint (§8, `indicators.BollingerBands`).
- **Вход** (§9.2–9.3): пробой полосы + гейт отклонения от средней.
  - `SELL` (шорт спреда) — пробой верхней полосы; `BUY` (лонг спреда) — нижней.
  - Гейт `AbsOfMean` (по умолчанию): `|cur − SMA| ≥ DeviationPct·|SMA|` — **знаконезависим**,
    корректен при любом знаке спреда (исправляет баг исходного `SMA·1.02` при SMA<0).
- **Выход** (§9.4): пересечение живой SMA (или зафиксированной при `freeze_sma_on_exit`).
  Опциональный защитный стоп `stop_sigma` (по умолчанию выключен — буква §9.4).
- **FSM** (§9.1, `engine.TradingEngine`): `FLAT → ENTERING_* → SHORT/LONG_SPREAD → EXITING → FLAT`,
  аварийный `HALTED`. Повторный вход запрещён до возврата в FLAT.
- **Исполнение** (§10, `execution.OrderExecutor`): атомарная пара, менее ликвидную ногу (SBPR)
  заливаем первой; при срыве второй — аварийный unwind первой; если невозможно — `HALTED`.
- **P&L** (§9.5): по тикам, `(exit−entry)·dir·lots·(TickValue/TickSize)` — `STEPPRICE/MINSTEP`
  из спецификации инструмента (не хардкод). На `LOTVOLUME` не умножаем — `STEPPRICE` уже
  на целый контракт (иначе P&L завышается в LOTVOLUME раз).
- **Риск** (§11, `risk.RiskManager`): `MaxOpenPositions=1`, дневной лимит убытка, серия ошибок →
  `HALTED`, flat-all, reconciliation на старте.

## ⚠ Знак направления ног (важно)

§2 ТЗ задаёт «шорт спреда = продать SBRF + купить SBPR», но это математически неверно:
`buy SBPR + sell SBRF` даёт `+exposure` к спреду (выигрыш при РОСТЕ). Реализован корректный
вариант: **SELL-сигнал → шорт спреда = `buy SBRF + sell SBPR`** (ставка на падение спреда),
**BUY → лонг спреда = `sell SBRF + buy SBPR`**. Тогда P&L = ±(spread_exit − spread_entry),
знак согласован с направлением (см. `engine._open_position`, тест `test_short_spread_profits_when_spread_falls`).

## Данные — MOEX ISS (публичный REST, без ключей)

`data_feed.py`: тянет 10-мин свечи фьючерсов FORTS (`engine=futures/market=forts`), авто-роллировер
ближайшей ликвидной серии по `LASTTRADEDATE`. **5-мин свечей в ISS нет** (интервалы 1/10/60),
дефолт st4 — 10m. Спецификация инструмента: `MINSTEP`, `STEPPRICE`, `LOTVOLUME`.

## API (`/st4/*`, см. api.py)

`state`, `config`, `control/{start,stop,flat-all,trading,resume}`, `player/{start,stop}`,
`approve`, `reject`, `auto`, `reset`, `trades`, `backtest`, `tests`, `connector`.

## Исполнение: paper vs T-Bank sandbox (Phase 2)

Исполнитель пары выбирается по `cfg.connector.mode`:
- `paper` (по умолчанию) — `execution.OrderExecutor`, виртуальные филлы по ценам бара.
- `tbank_sandbox` — `tinkoff_executor.TinkoffSandboxExecutor`, **реальные ордера в песочнице
  T-Bank** (виртуальный счёт, виртуальные деньги) через `tbank_sandbox.py`. Вход и выход —
  настоящие market-ордера; счёт T-Bank и движок синхронны.

Engine зависит только от интерфейса `PairExecutor` (`execute_pair` + `close_pair`) — фабрика
в `TradingEngine.__init__`. Переключение режима и **ввод API-токена** — на вкладке st4
(блок «Коннектор») или `POST /st4/connector {mode, token}`.

**Правила/безопасность:**
- Токен — только в окружении процесса (`TBANK_TOKEN`), не на диске, не в git, не в snapshot
  (отдаётся лишь булев `token_set`). UI очищает поле токена после применения.
- Sandbox активен **только в live** (MOEX ISS). На синтетике исполнение всегда paper
  (`sandbox_active=false`). При недоступности sandbox (нет токена/сети) — авто-откат в paper.
- Только `SandboxService.*` — боевой `OrdersService` не реализован, реальный биржевой ордер
  отправить нельзя (жёсткое ограничение проекта).
- Тикеры T-Bank = коды серий FORTS (SRM6/SPM6); P&L по `STEPPRICE/MINSTEP`.

Известное упрощение (вне scope): `reconcile` в sandbox мог бы сверяться с `positions(account_id)`.

## ⚠ Про 100% win-rate в бэктесте

На спокойном периоде win-rate близок к 100% — SBRF/SBPR жёстко коинтегрированы (один эмитент),
спред почти всегда возвращается к средней. Честный риск — в `max_drawdown_pct` (считается по equity
с **нереализованным** P&L открытой позиции) и в том, что без `stop_sigma` позиция может зависнуть
до возврата к средней. Это свойство стратегии ТЗ (выход только по средней), а не баг.

## Тесты

`tests/test_st4.py` — индикатор (эталон pandas), синхронизация/пропуски, гейт §9.3 (вкл.
отрицательный спред), сигналы §9.2/9.4, знак P&L, атомарность/unwind/HALTED, reconciliation,
дневной лимит, клиринговые окна, бэктест-метрики.
