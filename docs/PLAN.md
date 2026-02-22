# План

## In Progress

- [ ] Начальная структура проекта и документация

## TODO

### Phase 1: Core Infrastructure
- [ ] Настроить Python проект (pyproject.toml, ruff, mypy)
- [ ] Реализовать базовые модели данных (Candle, Order, Trade)
- [ ] Создать ExchangeManager с адаптерами Binance/Bybit/OKX
- [ ] Настроить Redis + TimescaleDB (docker-compose)
- [ ] Реализовать WebSocket коннекторы для real-time данных

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

