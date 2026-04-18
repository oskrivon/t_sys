# Архитектура

## Обзор

Trading Infrastructure — гибкая платформа для анализа криптовалютных бирж, бэктестинга стратегий и автоматической торговли. Дизайн ориентирован на модульность: каждый компонент можно заменить или расширить независимо.

## Стек

- **Runtime:** Python 3.11+ (asyncio)
- **Exchange API:** ccxt-async (100+ бирж), websockets
- **Database:** TimescaleDB (time-series), Redis (cache, pub/sub)
- **API:** FastAPI + uvicorn
- **AI:** anthropic (Claude), openai
- **Messaging:** Telegram Bot API
- **Future:** Rust core для HFT (если потребуется <1ms латентность)

## Структура кодовой базы

```
src/
├── core/                    # Ядро системы ✅
│   ├── config.py            # ✅ Settings через pydantic-settings
│   ├── exchange/            # ✅ CCXT обёртки, унификация API
│   │   ├── base.py          # ✅ Абстрактный ExchangeAdapter
│   │   ├── ccxt_adapter.py  # ✅ Реализация через CCXT
│   │   └── manager.py       # ✅ ExchangeManager (multi-exchange)
│   ├── websocket/           # 🔲 WS менеджеры для real-time
│   └── models/              # ✅ Pydantic модели
│       └── base.py          # ✅ Ticker, Candle, Order, Trade, Balance, etc.
├── data/                    # Слой данных
│   ├── collectors/          # 🔲 Асинхронные сборщики
│   ├── storage/             # 🔲 TimescaleDB, Redis адаптеры
│   └── cache/               # 🔲 Кэш orderbook, тикеров
├── strategy/                # Стратегии и анализ
│   ├── models.py            # ✅ Level, Signal, SignalType, ScreenerResult
│   ├── levels.py            # ✅ Swing points, clustering, rolling levels
│   ├── signals.py           # ✅ Breakout/retest/zakol detection
│   ├── features.py          # ✅ 29 ML features computation
│   ├── backtester/          # 🔲 Event-driven бэктестер
│   ├── patterns/            # 🔲 Детекторы паттернов
│   └── signals/             # 🔲 Генераторы сигналов
├── execution/               # Торговля
│   ├── orders/              # 🔲 Order management system
│   ├── risk/                # 🔲 Position sizing, limits
│   └── arbitrage/           # 🔲 Cross-exchange арбитраж
├── screener/                # ✅ Real-time signal scanner
│   ├── config.py            # ✅ ScreenerConfig (pydantic-settings)
│   ├── scanner.py           # ✅ MiroScreener: run_once/run_forever
│   ├── coins_in_play.py     # ✅ Volume spike detector
│   ├── data_fetcher.py      # ✅ Async batched OHLCV
│   └── state.py             # ✅ JSON state persistence
├── ai/                      # AI интеграции
│   ├── ml_scorer.py         # ✅ GradientBoosting P(win) predictor
│   ├── vision_scorer.py     # ✅ Claude Vision via OpenRouter
│   ├── chart_generator.py   # ✅ Candlestick chart PNG
│   ├── analyzer/            # 🔲 LLM анализ ситуаций
│   └── monitor/             # 🔲 Автоматический мониторинг
└── api/                     # Интерфейсы
    ├── rest/                # 🔲 FastAPI endpoints
    ├── telegram/            # ✅ Signal alert bot
    │   ├── bot.py           # ✅ TelegramNotifier
    │   └── formatters.py    # ✅ Message formatting
    └── dashboard/           # 🔲 Web UI (позже)

scripts/
└── test_connection.py       # ✅ Тест подключения к биржам

Легенда: ✅ Реализовано | 🔲 Планируется
```

## Ключевые компоненты

### 1. Exchange Layer (src/core/exchange/)
```
ExchangeManager
├── BinanceAdapter
├── BybitAdapter
└── OKXAdapter

Каждый адаптер реализует:
- get_orderbook()
- get_ticker()
- get_candles()
- place_order()
- cancel_order()
- get_balance()
```

### 2. Data Layer (src/data/)
```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  Collector  │────▶│   Redis     │────▶│ TimescaleDB │
│  (WS/REST)  │     │  (hot data) │     │ (cold data) │
└─────────────┘     └─────────────┘     └─────────────┘
                          │
                          ▼
                    ┌─────────────┐
                    │  Consumers  │
                    │ (Strategy)  │
                    └─────────────┘
```

### 3. Strategy Layer (src/strategy/)
```
Signal Flow:
MarketData → PatternDetector → SignalGenerator → RiskFilter → OrderManager
```

### 4. Execution Layer (src/execution/)
```
OrderManager
├── validate_order()      # Проверка лимитов
├── execute_order()       # Отправка на биржу
├── monitor_order()       # Отслеживание статуса
└── handle_fill()         # Обработка исполнения

RiskManager
├── check_position_size()
├── check_daily_loss()
├── check_correlation()
└── emergency_stop()
```

## База данных

### TimescaleDB (PostgreSQL + time-series)

```sql
-- Свечи
CREATE TABLE candles (
    time        TIMESTAMPTZ NOT NULL,
    exchange    TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    timeframe   TEXT NOT NULL,
    open        DECIMAL,
    high        DECIMAL,
    low         DECIMAL,
    close       DECIMAL,
    volume      DECIMAL,
    PRIMARY KEY (time, exchange, symbol, timeframe)
);
SELECT create_hypertable('candles', 'time');

-- Сделки
CREATE TABLE trades (
    id          BIGSERIAL,
    time        TIMESTAMPTZ NOT NULL,
    exchange    TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,
    price       DECIMAL NOT NULL,
    amount      DECIMAL NOT NULL,
    ...
);

-- Ордера
CREATE TABLE orders (...);
```

### Redis

```
Keys:
  orderbook:{exchange}:{symbol}     # JSON orderbook
  ticker:{exchange}:{symbol}        # Последний тикер
  position:{exchange}:{symbol}      # Текущая позиция
  signal:queue                      # Очередь сигналов (list)
  lock:{resource}                   # Distributed locks
```

## Ключевые паттерны

1. **Async-first** — всё через asyncio, никаких блокирующих вызовов
2. **Event-driven** — компоненты общаются через события (Redis pub/sub)
3. **Adapter pattern** — биржи за единым интерфейсом
4. **Strategy pattern** — стратегии реализуют общий интерфейс
5. **Circuit breaker** — защита от каскадных сбоев API

## Зависимости между модулями

```
api/ ──────────────────────────────────────────┐
  │                                            │
  ▼                                            ▼
execution/ ◀──────── strategy/ ◀──────── ai/
  │                     │
  ▼                     ▼
core/exchange/ ◀───── data/
  │                     │
  └────────┬────────────┘
           ▼
    External APIs
   (Binance, Bybit, OKX)
```

**Критичные зависимости:**
- `execution/` зависит от `core/exchange/` — менять осторожно
- `data/storage/` — центральная точка, тестировать тщательно
- Конфиги в `config/` — единый источник истины
