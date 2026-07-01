# Runbook

## Сервер

```
Host:     <SERVER_HOST>
User:     <SERVER_USER>
SSH:      ssh <SERVER_USER>@<SERVER_HOST>
```

Все значения хоста и креды — только через `.env` (`SERVER_HOST`, `SERVER_USER`, `SERVER_PASSWORD`).

### Структура на сервере

```
/root/trading/          # Рабочая директория
/root/trading/tmp/      # Временные скрипты и данные (не коммитятся)
```

## Platform (Docker Compose)

```bash
# Запуск всех сервисов
docker compose -f docker-compose.platform.yml up -d

# Логи конкретного сервиса
docker compose -f docker-compose.platform.yml logs -f engine

# Перезапуск движка
docker compose -f docker-compose.platform.yml restart engine

# Стоп всего
docker compose -f docker-compose.platform.yml down
```

### Сервисы

| Сервис | Контейнер | Описание |
|--------|-----------|----------|
| redis | trading-redis | pub/sub + cache |
| screener-4h | miro-screener-4h | Скринер 4h + D1 levels |
| screener-1h | miro-screener-1h | Скринер 1h scalp |
| paper-trading | paper-trading | Paper trades от всех сигналов |
| engine | trading-engine | Торговый движок (funding capture) |
| telegram-bot | telegram-bot | Управление + алерты |

### Redis каналы

| Канал | От → Кому | Описание |
|-------|-----------|----------|
| signals:screener | screener → paper, engine | Сигналы скринера |
| commands:engine | telegram → engine | Команды управления |
| notifications:telegram | все → telegram | Уведомления в TG |
| events:trades | engine → paper, telegram | События сделок |

## Weekend Signal (cron)

**Стратегия:** 5-WAY VOTE(KWEB fri + EWJ fri + XLK week + XLE week + USDJPY week) -> BTC.
**Backtest OOS:** Sharpe_net 1.83, WR 64%, annual ~25%, MaxDD -5.5%.

### Cron jobs (на сервере)

```cron
# Friday signal (3 попытки с идемпотентностью)
5 21 * * 5   cd /root/trading && python3 scripts/weekend_signal.py friday >> data/logs/weekend.log 2>&1
10 21 * * 5  cd /root/trading && python3 scripts/weekend_signal.py friday >> data/logs/weekend.log 2>&1
15 21 * * 5  cd /root/trading && python3 scripts/weekend_signal.py friday >> data/logs/weekend.log 2>&1

# SL check каждые 4 часа в субботу-воскресенье
0 */4 * * 6  cd /root/trading && python3 scripts/weekend_signal.py check-sl >> data/logs/weekend.log 2>&1
0 */4 * * 0  cd /root/trading && python3 scripts/weekend_signal.py check-sl >> data/logs/weekend.log 2>&1

# Sunday settlement
5 23 * * 0   cd /root/trading && python3 scripts/weekend_signal.py settle >> data/logs/weekend.log 2>&1
```

### Команды

```bash
# Ручной dry-run (последняя пятница)
python3 scripts/weekend_signal.py friday --dry-run --no-telegram

# Историческая дата
python3 scripts/weekend_signal.py friday --historical 2026-04-18 --no-telegram

# История трейдов
python3 scripts/weekend_signal.py history

# Проверить SL вручную
python3 scripts/weekend_signal.py check-sl --no-telegram
```

### При падении

- **yfinance down:** 3 retry с exponential backoff. Если все fail — cron retry через 5/10 мин. Trade не открывается.
- **Telegram down:** Trade записан в DB, алерт не отправлен. Ошибка в логах.
- **Server reboot mid-weekend:** Sunday settle cron найдет open trade в SQLite и закроет нормально.
- **Duplicate cron:** `signal_date UNIQUE` в DB — второй запуск = no-op.

### Конфигурация

Предикторы и параметры в `src/weekend/config.py` → `DEFAULT_CONFIG`.
Telegram credentials в `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).
DB: `data/paper_trades.db` (таблица `weekend_trades`).

## Daily Digest (cron)

**Что:** Health check всех сервисов + stats по стратегиям → Telegram.

```cron
0 8 * * *    cd /root/trading && python3 scripts/daily_report.py --telegram >> data/logs/daily_report.log 2>&1
```

**Покрывает:**
- Docker containers status + Redis
- Engine restart count (WARNING если >3/24h)
- **Icebreaker (paper):** liveness по heartbeat (WARNING если beat >180s — демон завис/умер) + open positions + paper P&L 24h/all-time + last signal
- **Strategies (cron):** last-activity weekend/calendar/v-bottom (информативно, не алармы — event-driven)
- Funding capture: 24h trades, WR, funding earned, staleness warning
- Screeners: scan count, alerts, ML filtered
- Volume ranking: paper targets

Liveness ледокола держится на heartbeat-файле `data/icebreaker_paper_heartbeat.json`, который демон перезаписывает каждый цикл (~20s). Демон должен стартовать с `--heartbeat /root/trading/data/icebreaker_paper_heartbeat.json` (абсолютный путь: демон работает из worktree `/root/trading_ib`, а дайджест читает из `/root/trading`); это уже в `@reboot`-cron. Если секция показывает `Daemon: DOWN` или `STALE` — перезапуск см. в описании icebreaker paper-демона.

```bash
# Ручной запуск (консоль)
python3 scripts/daily_report.py

# Ручной запуск (Telegram)
python3 scripts/daily_report.py --telegram
```

## Calendar Signal (cron)

**Стратегия:** Pre-FOMC LONG (8h before decision) + Post-Q-Expiry SHORT (24h after quarterly options expiry).
**Backtest OOS:** Combined Sharpe_net 1.06, WR 63%, annual ~14%, 20 trades/yr.

### Cron jobs (на сервере)

```cron
# FOMC entry (usually Wednesday, but script checks event list)
0 10 * * 3   cd /root/trading && python3 scripts/calendar_signal.py entry >> data/logs/calendar.log 2>&1
5 10 * * 3   cd /root/trading && python3 scripts/calendar_signal.py entry >> data/logs/calendar.log 2>&1

# Q-expiry entry (last Friday of Mar/Jun/Sep/Dec — script self-filters)
0 8 * * 5    cd /root/trading && python3 scripts/calendar_signal.py entry >> data/logs/calendar.log 2>&1
5 8 * * 5    cd /root/trading && python3 scripts/calendar_signal.py entry >> data/logs/calendar.log 2>&1

# Exit check every 4h (handles both 8h FOMC and 24h expiry holds)
0 */4 * * *  cd /root/trading && python3 scripts/calendar_signal.py exit >> data/logs/calendar.log 2>&1
```

### Команды

```bash
python3 scripts/calendar_signal.py next              # upcoming events
python3 scripts/calendar_signal.py entry --dry-run    # test entry
python3 scripts/calendar_signal.py history            # trade history
```

### Обновление дат

FOMC и Q-expiry даты захардкожены в `src/calendar_signal/config.py`. Обновлять раз в год при публикации нового календаря ФРС.

## CI/CD (GitHub Actions)

Настроен в `.github/workflows/ci.yml`. На push в master:

1. **test** — pytest + coverage (min 30%)
2. **changes** — определяет какие сервисы затронуты:
   - `src/core/`, `src/execution/`, `src/strategies/`, `Dockerfile` → **engine**
   - `src/screener/`, `src/strategy/`, `src/ai/` → **screener-4h, screener-1h**
3. **deploy** — SSH на сервер → `git pull` → `docker compose up --build --no-deps <services>`

Деплой автоматический, ручной рестарт **не нужен**. Просто push в master.

Secrets в GitHub: `SERVER_HOST`, `SERVER_SSH_KEY`.

## Окружение

- **Python:** 3.11+
- **Database:** TimescaleDB (PostgreSQL 15+), Redis 7+
- **OS:** Windows (локально) / Linux (сервер)

## Установка

```bash
# 1. Клонировать репозиторий
cd C:\trading

# 2. Создать виртуальное окружение
python -m venv venv
.\venv\Scripts\activate  # Windows
# source venv/bin/activate  # Linux/macOS

# 3. Установить зависимости
pip install -e ".[dev]"

# 4. Скопировать конфиг
cp config/example.env .env
# Заполнить API ключи в .env

# 5. Запустить базы данных
docker-compose up -d
```

## Запуск

```bash
# Тест подключения к биржам (публичные данные, без ключей)
python scripts/test_connection.py

# Тест конкретной биржи
python scripts/test_connection.py binance
python scripts/test_connection.py bybit
python scripts/test_connection.py okx

# Основной запуск (TODO)
python -m src.main

# Collector (TODO)
python -m src.data.collectors.main

# API сервер (TODO)
uvicorn src.api.rest.main:app --reload

# Telegram бот (TODO)
python -m src.api.telegram.bot
```

## Docker

```bash
# Поднять инфраструктуру
docker-compose up -d

# Логи
docker-compose logs -f

# Остановить
docker-compose down
```

## Сервисы

| Сервис | Порт | Команда |
|--------|------|---------|
| TimescaleDB | 5432 | `docker-compose up -d timescaledb` |
| Redis | 6379 | `docker-compose up -d redis` |
| API | 8000 | `uvicorn src.api.rest.main:app` |

## Конфигурация

Все настройки через переменные окружения (.env):

```env
# Биржи
BINANCE_API_KEY=...
BINANCE_SECRET=...
BYBIT_API_KEY=...
BYBIT_SECRET=...
OKX_API_KEY=...
OKX_SECRET=...
OKX_PASSPHRASE=...

# Database
DATABASE_URL=postgresql://user:pass@localhost:5432/trading
REDIS_URL=redis://localhost:6379

# AI
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...

# Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

## Мониторинг

- **Логи:** `logs/` директория, ротация по размеру
- **Метрики:** TODO (Prometheus + Grafana)
- **Алерты:** Telegram бот

## Частые проблемы

| Симптом | Причина | Решение |
|---------|---------|---------|
| `ccxt.AuthenticationError` | Неверные API ключи | Проверить .env, проверить IP whitelist на бирже |
| `asyncio.TimeoutError` | Биржа не отвечает | Проверить VPN/прокси, увеличить timeout |
| `Redis connection refused` | Redis не запущен | `docker-compose up -d redis` |
| `TimescaleDB connection refused` | DB не запущена | `docker-compose up -d timescaledb` |
