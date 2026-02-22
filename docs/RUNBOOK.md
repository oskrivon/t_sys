# Runbook

## Окружение

- **Python:** 3.11+
- **Database:** TimescaleDB (PostgreSQL 15+), Redis 7+
- **OS:** Windows / Linux / macOS

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
