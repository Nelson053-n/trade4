# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это

trade4 — самостоятельный сервис торговой стратегии **st4**: статистический арбитраж спреда
фьючерсов FORTS на обыкновенные и привилегированные акции одного эмитента (SBRF/SBPR и др.).
Данные — публичный MOEX ISS (без ключей). Backend на FastAPI + одностраничный canvas-дашборд.
Выделен из общего проекта `trade` в отдельный сервис (домен trade4.bananagen.ru, порт 8001),
**не конфликтует** с `trade.service` (порт 8000).

Фаза проекта: paper / T-Bank sandbox + опциональный боевой контур `tbank_real` под тройным гейтом.

## Команды

```bash
pip install -r requirements.txt              # зависимости (в .venv)
uvicorn app.api:app --reload --port 8001     # backend + панель на http://127.0.0.1:8001
pytest -q                                     # все тесты движка
pytest tests/test_st4.py::test_<name> -q      # один тест
```

Прод-деплой (systemd + nginx) и DNS описаны в `README.md`; конфиги — в `infra/`.

## Архитектура

Поток данных однонаправленный: **свечи → спред-бар → BB-индикатор → сигнал FSM → исполнитель пары → позиция/P&L/риск**.

- `app/api.py` — FastAPI. Все торговые эндпоинты под `/st4/*` (см. список роутов в файле).
  Отдаёт `dashboard.html` на `/`, `login.html` на `/login`. Содержит свой слой **авторизации**:
  логин/пароль из `TRADE4_USER`/`TRADE4_PASS` + подписанная HMAC cookie. Авторизация
  **включается только когда заданы обе переменные** — без них (локальная разработка, тесты)
  middleware пропускает всё.
- `app/st4/service.py` — сервисный слой. `St4Session` держит движок, конфиг, историю графика,
  фоновые задачи (live-поток с MOEX ISS / player-синтетику). Состояние переживает рестарт через
  `session_state_4*.json` (журнал сделок, баланс, время сессии). **`ST4_PAIRS`** — реестр
  доступных пар (sber/sngr/rtkm/tatn); per-pair настройки стратегии берутся из КОДА, не из
  session-файла.
- `app/st4/engine.py` — `TradingEngine`, конечный автомат (§9.1):
  `FLAT → ENTERING_* → SHORT/LONG_SPREAD → EXITING → FLAT`, аварийный `HALTED`. Метод `step(bar)`
  возвращает `StepResult`. Зависит только от интерфейса `PairExecutor` (фабрика в `__init__`).
- `app/st4/indicators.py` — `SpreadBuilder` (синхронизация двух ног по бару), `BollingerBands`
  (SMA200/2σ, population-σ, no-repaint), `VolumeAverage`.
- `app/st4/strategy.py` — чистые функции сигналов: `entry_signal`, `exit_signal`, `in_clearing_window`.
- `app/st4/execution.py` — `OrderExecutor` (paper): атомарная пара, unwind при отказе второй ноги,
  `leg_pnl_rub` / `pair_fee_rub`.
- `app/st4/risk.py` — `RiskManager`: MaxOpenPositions=1, дневной лимит убытка, серия ошибок → HALTED.
- `app/st4/data_feed.py` — MOEX ISS REST: 10m/60m свечи FORTS, авто-роллровер ближней серии,
  спецификация инструмента (MINSTEP/STEPPRICE/LOTVOLUME).
- `app/st4/models.py` — dataclasses/enums (`BotState`, `Signal`, `Role`, `Position`, `Trade`, …).
- `app/st4/backtest.py`, `scan_pairs.py` — бэктест и сканер пар-кандидатов.
- `dashboard.html` — премиум-терминал, **один самодостаточный файл, canvas-графики без CDN**,
  никакой сборки и npm. Правится напрямую. Структура: `<style>` (~11–231) → HTML-разметка
  (~233–504) → `<script>` (~504–1240). Все JS-функции с префиксом **`s4*`**; точки входа:
  `s4Init` (старт), `s4Poll`/`s4Render` (опрос и рендер `/st4/state`), `s4DrawChart`/`s4Redraw`
  (canvas-перерисовка графика спреда — звать после смены данных/темы/шрифта), `s4SwitchPair`
  (переключение пары). Состояние UI (тема, шрифт, выбранные боевые пары) — в `localStorage`.

### Исполнители пары (выбор по `cfg.connector.mode`)

Движок работает через интерфейс `PairExecutor` (`execute_pair` + `close_pair`):
- `paper` — `execution.OrderExecutor`, виртуальные филлы по ценам бара (по умолчанию).
- `tbank_sandbox` — `tinkoff_executor.TinkoffSandboxExecutor` поверх `tbank_sandbox.py`:
  реальные market-ордера в **песочнице** T-Bank (виртуальные деньги). Только `SandboxService.*`.
- `tbank_real` — `tbank_live.py`: **боевой** контур (реальные деньги). REST-слой
  (хост/токен/SSL/ретраи/котировки/справочник) переиспользуется из `tbank_sandbox.py`,
  разделение только по namespace метода.

## Критичные инварианты (не сломать)

- **Знак направления ног** (`engine._open_position`): SELL-сигнал → шорт спреда = `buy SBRF + sell SBPR`
  (ставка на падение спреда); BUY → лонг = `sell SBRF + buy SBPR`. Тогда `P&L = ±(spread_exit − spread_entry)`.
  Реализация сознательно **отличается от §2 ТЗ** (там математически неверно). См. тест
  `test_short_spread_profits_when_spread_falls`. Подробности — `app/st4/README.md`.
- **P&L по тикам** (§9.5): `(exit−entry)·dir·lots·(STEPPRICE/MINSTEP)` из спецификации инструмента,
  **не хардкод**. На `LOTVOLUME` НЕ умножать — STEPPRICE уже на целый контракт.
- **Гейт входа** `AbsOfMean`: `|cur − SMA| ≥ DeviationPct·|SMA|` — знаконезависим (корректен при SMA<0).
- **Боевой ордер** (`tbank_live.post_order`) тратит реальные деньги — вызывается только при тройном
  гейте: `mode==tbank_real` И `real_trading_armed` И `trading_enabled`.
- **Токен T-Bank** — только в окружении процесса (`TBANK_TOKEN`) или файле `app/st4/.tbank_token`.
  Никогда в git/snapshot: наружу отдаётся лишь булев `token_set`.
- Sandbox/real исполнение активно **только в live** (MOEX ISS). На синтетике (player) — всегда paper.
- `session_state_4*.json`, `.tbank_token`, `out/` — рантайм/секреты, в `.gitignore`, не коммитить.

## Стиль

Код и комментарии — на русском, как в существующих файлах. Многие комментарии ссылаются на
разделы ТЗ (§7–§11) — сохраняй эти привязки при правках движка. Соблюдай Surgical Changes
из глобального CLAUDE.md.
