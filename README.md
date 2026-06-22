# trade4 — спред-арбитраж обычка/преф на FORTS

Самостоятельный сервис стратегии **st4**: статистический арбитраж спреда фьючерсов на
обыкновенные и привилегированные акции одного эмитента (FORTS, данные MOEX ISS, без ключей).
Выделен из общего проекта `trade` в отдельный сервис со своим премиум-дашбордом и доменом
**trade4.bananagen.ru**.

**Phase 1 — paper / T-Bank sandbox. Реальные ордера на бирже не выставляются.**

## Что внутри
- `app/st4/` — движок: FSM (BB200, пробой полос → вход, возврат к SMA → выход, стоп по σ),
  атомарные пары (unwind при отказе второй ноги), reconciliation, бэктест, скан пар,
  T-Bank sandbox-исполнитель. Подробности — `app/st4/README.md`.
- `app/api.py` — FastAPI backend: панель на `/`, API на `/st4/*`.
- `dashboard.html` — премиум-дашборд (тёмный трейдинг-терминал, canvas-графики без CDN).
- `tests/test_st4.py` — 48 юнит-тестов движка.
- `infra/` — systemd-сервис и nginx-конфиг для прод-деплоя.

## Торговые пары (обычка / преф)
| id | обычка / преф | интервал | примечание |
|------|---------------|----------|------------|
| `sber` | SBRF / SBPR | 10m | ликвидная, дефолтная стратегия BB(200) |
| `sngr` | SNGR / SNGP | 10m | узкий туннель Sigma-гейтом (σ1.5/sma60) |
| `rtkm` | RTKM / RTKMP | 60m | преф тонок на 10m → только часовой |
| `tatn` | TATN / TATP | 60m | **новая**: STEPPRICE 1₽ у обеих ног, ratio≈1; пороги стартовые — откалибровать бэктестом |

Кандидаты BANE/BANEP, NKNC/NKNCP, MTLR/MTLRP отвергнуты: преф-фьючерсы на FORTS неликвидны
(нет ближней серии). Проверено на реальных данных MOEX ISS.

## Команды
```bash
pip install -r requirements.txt                  # зависимости
uvicorn app.api:app --reload --port 8001         # backend + панель (http://127.0.0.1:8001)
pytest -q                                         # тесты (48)
```

## Деплой (прод, /opt/trade4, порт 8001)
```bash
# systemd
sudo cp infra/trade4.service /etc/systemd/system/trade4.service
sudo systemctl daemon-reload && sudo systemctl enable --now trade4
# nginx (домен trade4.bananagen.ru → 127.0.0.1:8001)
sudo cp infra/nginx-trade4.bananagen.ru.conf /etc/nginx/sites-available/trade4.bananagen.ru
sudo ln -s /etc/nginx/sites-available/trade4.bananagen.ru /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
# DNS: A-запись trade4.bananagen.ru → IP сервера (прописать после готовности)
```
Не конфликтует с `trade.service` (порт 8000, домен trade.bananagen.ru).

## Секреты
- Токен T-Bank — через `TBANK_TOKEN` (env / systemd drop-in) либо файл `app/st4/.tbank_token`.
  **Никогда не коммитить** (в `.gitignore`).
