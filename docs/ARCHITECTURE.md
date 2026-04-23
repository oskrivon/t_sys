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
│   ├── redis_bus.py         # ✅ Redis pub/sub межсервисная шина
│   ├── websocket/           # ✅ WS менеджеры для real-time
│   │   ├── base.py          # ✅ WebSocketFeed ABC
│   │   └── bybit_ws.py      # ✅ Bybit V5 (public + private, auto-reconnect)
│   └── models/              # ✅ Pydantic модели
│       ├── base.py          # ✅ Ticker, Candle, Order, Trade, Balance, etc.
│       └── signals.py       # ✅ ScreenerSignal, FundingSignal, TradeEvent, EngineCommand
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
├── strategies/              # ✅ Multi-strategy layer (N стратегий)
│   ├── base.py              # ✅ Strategy ABC, TradeSignal, TargetPosition
│   ├── miro_strategy.py     # ✅ Miro S/R + ML + Vision (event-driven)
│   ├── volume_ranking.py    # ✅ Volume Ranking L/S (systematic daily)
│   └── funding_capture.py   # ✅ Funding rate capture (WS-driven, >10bps)
├── portfolio/               # ✅ Portfolio management
│   └── manager.py           # ✅ PortfolioManager: allocation, risk, conflicts
├── engine/                  # ✅ Trading engine daemon
│   ├── daemon.py            # ✅ TradingEngine: main daemon, TaskGroup
│   ├── event_bus.py         # ✅ EventBus: typed async pub/sub
│   ├── scheduler.py         # ✅ StrategyScheduler (4h/daily aligned)
│   └── state.py             # ✅ StateManager (SQLite persistence)
├── paper_trading/           # ✅ Paper trading infrastructure
│   ├── db.py                # ✅ SQLite CRUD for paper trades
│   ├── tracker.py           # ✅ PaperTrader: record signals, check TP/SL
│   ├── service.py           # ✅ Standalone paper trading service (Redis subscriber)
│   └── stats.py             # ✅ Statistics: WR, PnL, PF, equity
├── execution/               # ✅ Торговля
│   ├── executor.py          # ✅ ExecutionManager: orders, TP/SL, funding exit
│   ├── position_tracker.py  # ✅ PositionTracker: in-memory + exchange sync
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
    ├── telegram/            # ✅ Telegram bot (bidirectional)
    │   ├── bot.py           # ✅ TelegramNotifier (outbound alerts)
    │   ├── service.py       # ✅ TelegramService (commands + Redis forwarding)
    │   ├── commands.py      # ✅ Command handlers (/status, /positions, etc.)
    │   └── formatters.py    # ✅ Message formatting
    └── dashboard/           # 🔲 Web UI (позже)

scripts/
├── run_engine.py            # ✅ Trading engine daemon
├── run_screener.py          # ✅ Miro screener (4h loop)
├── run_telegram.py          # ✅ Telegram bot service
├── run_paper_trading.py     # ✅ Paper trading service
├── run_volume_ranking.py    # ✅ Volume Ranking daily rebalance
├── run_portfolio.py         # ✅ Multi-strategy runner
├── paper_trading.py         # ✅ Paper trading CLI (status/check/stats)

config/
└── strategies.yml           # ✅ Strategy config (what to trade, params)
├── daily_report.py          # ✅ Portfolio report (console + Telegram)
├── services/                # ✅ Windows Task Scheduler .bat wrappers
└── research/                # ✅ 15+ backtest/research scripts

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

## Cross-module contracts

Rules to prevent silent integration bugs (learned from the precompute incident — executor expected `_precomputed_qty` in signal metadata, strategy never set it, fallback silently traded min_qty for weeks).

### 1. Producer + consumer = one commit + contract test

When module A writes a field that module B reads, **both sides and a contract test must land in the same commit**. If staging drops one side, the test fails.

Example: executor checks `signal.metadata["_precomputed_qty"]` → the test that asserts `"_precomputed_qty" in signal.metadata` must exist in the same commit.

### 2. Fallbacks must be loud

If a code path has a fast path and a slow/degraded fallback, the fallback **must log a warning**. Silent fallbacks hide bugs for weeks.

```python
# Bad — silently degrades
qty = precomputed_qty or await self._compute_qty(symbol, 0)

# Good — screams in logs
if precomputed_qty:
    qty = precomputed_qty
else:
    logger.warning("precompute_missing", symbol=symbol)
    qty = await self._compute_qty(symbol, target_notional)
```

### 3. Test the intent, not the current code

Tests should encode **what the system should do**, not mirror what's currently committed. If the design says "entry at T-2s with precomputed qty", the test asserts precomputed qty in metadata — even if the code doesn't set it yet (test stays red as a reminder).

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
