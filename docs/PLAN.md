# План

## In Progress

## TODO

### Phase 1: Core Infrastructure (продолжение)
- [ ] Реализовать WebSocket коннекторы для real-time данных
- [ ] Запустить и протестировать docker-compose (TimescaleDB + Redis)

### Phase 2: Data Collection
- [ ] Collectors для orderbook, trades, candles
- [ ] Исторические данные — загрузка и хранение
- [ ] Кэширование в Redis

### Phase 3: Strategy & Backtesting
- [ ] Event-driven бэктестер
- [ ] Базовые паттерны из Miro (пробой, ретест, закол)
- [ ] Генератор сигналов

### Phase 4: Execution
- [ ] Order management system
- [ ] Risk manager
- [ ] Paper trading режим

### Phase 5: Arbitrage Infrastructure
- [ ] Cross-exchange spread monitor
- [ ] Arbitrage opportunity detector
- [ ] Execution с учётом комиссий и latency

### Phase 6: AI & Interface
- [ ] Claude API интеграция для анализа
- [ ] FastAPI endpoints
- [ ] Telegram бот

## Backlog

- DEX интеграция (Uniswap, dYdX)
- Rust core для HFT
- Web dashboard
- ML модели для предсказания
- Sentiment analysis (новости, соцсети)

## Done

### 2025-02-22 — Phase 1: Core Infrastructure (часть 1)
- [x] Настроить Python проект (pyproject.toml, ruff, mypy)
- [x] Реализовать Settings/Config через pydantic-settings
- [x] Реализовать базовые модели данных (Candle, Order, Trade, Ticker, Balance, Position, OrderBook)
- [x] Создать ExchangeAdapter (абстрактный интерфейс)
- [x] Создать CCXTAdapter (реализация через CCXT)
- [x] Создать ExchangeManager с адаптерами Binance/Bybit/OKX
- [x] Настроить docker-compose для Redis + TimescaleDB
- [x] Создать тестовый скрипт подключения (scripts/test_connection.py)
- [x] Документация по API ключам бирж (docs/EXCHANGES_SETUP.md)
- [x] Инициализация git репозитория

