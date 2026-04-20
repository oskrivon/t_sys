# Runbook

## Сервер

```
Host:     <SERVER_HOST>
User:     root
SSH:      ssh root@<SERVER_HOST>
```

Credentials в `.env` (`SERVER_PASSWORD`).

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
