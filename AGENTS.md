# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это

trade4 — сервис торговых стратегий на фьючерсах FORTS (данные — публичный MOEX ISS,
исполнение — T-Bank API). Backend на FastAPI + одностраничный canvas-дашборд (вкладки по
стратегиям). Выделен из общего проекта `trade` в отдельный сервис (домен trade4.bananagen.ru;
локально порт 8001, на проде 8004 — см. «Прод / инварианты»), **не конфликтует** с `trade.service`.

- **st4** — BB-спред-арбитраж обычка/преф одного эмитента (SBRF/SBPR…), FSM-движок.
- **st5** — statarb с Kalman-β/коинтеграцией, портфель до 3 пар, β-юниты ног,
  ЧЕСТНЫЙ P&L по фактическим ногам (α-баг учёта исправлен 02.07 — старые цифры несравнимы).
- **st6** — фандинг-арбитраж вечных фьючерсов: шорт перпа + лонг квартальника, дневная
  гранулярность, сигнал edge = фандинг − базис (SWAPRATE из ISS history).
- **st7** — «фандинг-давление»: полухедж-шорт перпа (2×+1×) при перегретом фандинге (>35пп).
- **st8** — дивидендный набег: лонг за ~10д до отсечки, выход накануне гэпа (+ пост-див шорт);
  календарь отсечек — `/st8/calendar`.
- **st9** — трендовая корзина: Donchian+ATR по перпам (60м) USDRUBF+IMOEXF, пирамидинг;
  GLDRUBF выведен флагом `entries_enabled`.

Фаза проекта: переход в бой (директива оператора: «всё пойдёт в бой, ошибки очень дорогие»).
Paper / T-Bank sandbox + боевой контур `tbank_real` под гейтом взвода
(`tbank_real`+`real_trading_armed`; вход дополнительно гейтится `trading_enabled` — см. «Критичные инварианты»).

## Прод / инварианты

- **Боевые деньги** — ошибки очень дороги; никаких «тестовых ордеров»/экспериментов на реальном счёте.
- Прод с 05.11.2026: **91.212.150.163** (vdsina; с vm-claude — `ssh amn-ru3`, root). Старый
  **212.8.228.248 выведен** после атаки (S3-бэкапы уничтожены) — не использовать, не деплоить туда.
- Юнит `trade4.service` (User=trade4) запускает `uvicorn sandbox_server:app` на **127.0.0.1:8004**
  (обёртка `infra/sandbox_server.py`, копия лежит в корне `/opt/trade4`): st8/st9 только на
  заданных sandbox-счетах, иначе старт прерывается до live-задач. Креды логина — `EnvironmentFile`
  `/etc/trade4/recovery.env`, токен — `app/st4/.tbank_token`. nginx — `/etc/nginx/conf.d/recovery-sites-*.conf`.
  На сервере ~10 чужих docker-сервисов и nginx других проектов — не трогать.
- **На проде нет `.git`**: код раскатан архивом коммита (сверка — sha256 файлов, см.
  `out/recovery_20261105/candidate_manifest.json` на vm-claude). `git pull` на проде неприменим.
- Активные стратегии с 05.11.2026: **st8 + st9 в T-Bank sandbox** (st4–st7 остановлены, боевой
  контур не взведён). Журналы до переезда не восстановлены (`history_recovered=false`), история
  операций — у брокера.
- **Бэкап**: на vm-claude cron каждый час (:17) `~/bin/trade4-prod-backup.sh` тянет с прода
  `session_state_*.json`, `recovery_metadata.json`, `out/`, юнит и nginx → `~/backups/trade4-prod/`
  (30 дней, лог `backup.log`). Секреты (токены, `/etc/trade4`) не копируются.
- **Не рестартовать/деплоить при открытых позициях.** Деплой — только через скилл **/trade-deploy**
  (флэт-гейт, бэкап, пост-проверки); статус/разбор дня — скилл **/trade-status**.
- **Истина = счёт T-Bank** (кэш/позиции), журналы движков могут врать — сверка через `GET /reconcile/daily`.
- Логин `/login` — **только JSON** (form-data → 500: на проде нет python-multipart).
- `session_state_*.json` и токены живут на `/opt/trade4` вне git — перед деплоем бэкапить через `cp`.
- Ожидание рыночных событий/флэта — **Monitor с until-условием**, не `sleep`/ручные циклы//loop.

## Команды

```bash
pip install -r requirements.txt              # зависимости (в .venv)
uvicorn app.api:app --reload --port 8001     # backend + панель на http://127.0.0.1:8001
pytest -q                                     # все тесты (test_st4/st5/st6/st7 + auth)
pytest tests/test_st5.py::test_<name> -q      # один тест
```

Прод-деплой — **только через скилл `/trade-deploy`** (не по README!; шаг выкладки под новый
сервер без git ещё не отлажен — согласовывать с оператором); статус прода — `/trade-status`.
Инфраструктура (systemd + nginx, DNS) описана в `README.md`; конфиги — в `infra/`.

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
- **Боевой ордер** (`tbank_live.post_order`) тратит реальные деньги. Гейт НА УРОВНЕ ОРДЕРА
  (executor `_post`, для ВСЕХ ордеров — вход/выход/flat/unwind): `mode==tbank_real` И
  `real_trading_armed` (+cooldown 600с). Дополнительно `trading_enabled` гейтит ТОЛЬКО ВХОД
  в новую позицию (st4 `risk.can_enter`, st5 `St5Portfolio.can_open`) — выходы/flat/усыновление
  от него НЕ зависят (сознательно: иначе при `trading_enabled=False` позиция залипнет — закрыть
  нельзя). Т.е. реальный гейт ордера ДВОЙНОЙ (`tbank_real`+`armed`); `trading_enabled` — третье
  условие лишь для открытия.
- **Токен T-Bank** — только в окружении процесса (`TBANK_TOKEN`) или файле `app/st4/.tbank_token`.
  Никогда в git/snapshot: наружу отдаётся лишь булев `token_set`. **Per-аккаунт токены**
  (st5/st6 на своих sandbox-токенах/счетах): мапа `app/st4/.tbank_account_tokens.json` —
  account-scoped вызовы `tbank_sandbox` берут токен счёта, env-токен — общий (приоритет env>файл).
- **P&L st5 — по ФАКТИЧЕСКИМ ногам** (β-юниты, `engine._legs_pnl`): пункт-стоимость каждой
  ноги, round-trip комиссии, half_spread в live-журнале. НЕ возвращать P&L «по спреду»:
  entry_spread — инновация Кальмана (с вычетом α), exit без α — α-баг завышал каждую сделку
  (см. память st5-alpha-bias-pnl-bug). Цифры журналов до 02.07.2026 несравнимы.
- **Голые ноги**: executor сверяет executed_lots (частичный филл → unwind реально налитого);
  на выделенном счёте (`dedicated_account=True`) periodic reconcile автозакрывает осиротевшие
  ноги. Pre-trade гейт: свободные деньги счёта против нотионала ордера (sandbox отклоняет
  по нотионалу!) с авто-даунсайзом юнитов.
- Sandbox/real исполнение активно **только в live** (MOEX ISS). На синтетике (player) — всегда paper.
- `session_state_*.json`, `.tbank_token*`, `out/` — рантайм/секреты, в `.gitignore`, не коммитить.

## Стиль

Код и комментарии — на русском, как в существующих файлах. Многие комментарии ссылаются на
разделы ТЗ (§7–§11) — сохраняй эти привязки при правках движка. Соблюдай Surgical Changes
из глобального CLAUDE.md.
