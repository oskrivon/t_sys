# План

## In Progress

### On-chain CEX-DEX arbitrage — Research (parallel track, $0)

Параллельно с funding, дополняет картину. Без смарт-контрактов. Подробности — `docs/ONCHAIN_ARB_RESEARCH.md`.

- [x] Setup free public RPC (publicnode.com, no Alchemy key needed)
- [x] Даунлоадер Uniswap v3 swaps через eth_getLogs (Ethereum L1, WETH/USDC)
- [x] Сопоставление с CEX bid/ask (Tardis March-1) — preliminary results
- [ ] Pool state analysis — sqrtPriceX96 между свопами vs CEX (реальные окна, не execution)
- [ ] Повторить для Arbitrum (L2: дешевле gas, меньше MEV, быстрее блоки)
- [ ] EOA swap prototype на Arbitrum testnet (web3.py)
- [ ] Backtest slow-arb strategy с real execution timing
- [ ] **Gate onchain-1:** решение — prototype или пивот

## TODO

### Triangular arbitrage — Research (side quest, ~1 неделя)
Low-priority мини-research. Вероятный результат — MMs забирают top-пары, но может быть потенциал на альткоин-треугольниках.

- [ ] Выбрать биржу + 3-5 alt треугольников (e.g. `USDT→SOL→ETH→USDT` на Bybit)
- [ ] Загрузить trades на 3 пары каждого треугольника за месяц
- [ ] Детектор triangular windows (внутри биржи, per-second)
- [ ] Сравнить с cross-exchange по длительности окон

### Phase 1: Core Infrastructure (продолжение)
- [ ] Реализовать WebSocket коннекторы для real-time данных
- [ ] Запустить и протестировать docker-compose (TimescaleDB + Redis)

### Phase 2: Data Collection
- [ ] Collectors для orderbook, trades, candles
- [ ] Исторические данные — загрузка и хранение
- [ ] Кэширование в Redis

### Phase 3: Strategy & Backtesting
- [ ] Event-driven бэктестер (переиспользуем для funding strategy)
- [ ] Базовые паттерны из Miro (пробой, ретест, закол)
- [ ] Генератор сигналов

### Phase 4: Execution
- [ ] Order management system (spot + perp)
- [ ] Risk manager (margin, liquidation, portfolio limits)
- [ ] Paper trading режим

### Phase 5: Cross-exchange Arbitrage Infrastructure (tail)

Cross-exchange spot арб перенесён в tail после Gate 2 (2026-04-17). Ok экономика (~20% APR с colocation), но требует всей остальной инфры. Подробности — `docs/ARBITRAGE_RESEARCH.md` → раздел "Gate 2 решение".

- [ ] Colocation setup: VPS в AWS Tokyo (Binance) + AWS Singapore (Bybit/OKX)
- [ ] WebSocket collector со всех 3 бирж, gap-detection, auto-reconnect
- [ ] Cross-exchange spread monitor
- [ ] Execution с минимальной latency (Python async, без Rust если возможно)
- [ ] Paper trading → measure real fill rate
- [ ] Capital ramp если fill rate ≥30%

### Phase 6: AI & Interface
- [ ] Claude API интеграция для анализа
- [ ] FastAPI endpoints
- [ ] Telegram бот

## Backlog

- DEX интеграция (Uniswap, dYdX) — частично откроем в on-chain research
- Rust core для HFT — **условно отложено**: exclusions см. ниже
- Web dashboard
- ML модели для предсказания
- Sentiment analysis (новости, соцсети)
- Cross-exchange arb с учётом transfer fees/времени (не pre-funded модель)
- Stablecoin arb (USDT/USDC/BUSD кросс-биржа)
- Exchange flows monitoring (netflow, whale tracking) — on-chain sub-track
- Proof-of-reserves мониторинг
- Atomic multi-DEX arb со смарт-контрактом (после успеха slow-arb)
- Flash loans + MEV-protected bundles (flashbots) — очень позже
- L2 orderbook анализ со slippage (если какой-то трек потребует)

### Почему Rust отложен
После Gate 2 (2026-04-17) установлено: главный bottleneck — сеть (50-300ms), не CPU (5ms в Python). Rust экономит единицы миллисекунд, но не делает экономически значимой разницы. Смысл появится только если мы:
1. Делаем colocation в DC биржи (latency <1ms) — тогда CPU становится bottleneck
2. Строим production HFT с собственной связью к matching engine

Ни то ни другое не относится к текущему roadmap.

## Done

### 2026-04-17 — Funding rate research Этап 1
- [x] Funding history download (20 pairs × 3 exchanges)
- [x] APR analysis + sign-flip distributions
- [x] Backtest threshold strategy + buy-and-hold + cross-exchange
- [x] Gate funding-1: RED for active trading, GREEN for passive yield

### 2026-04-17 — Gate 2 cross-exchange arb
- [x] Tardis free sample (2026-03-01) скачан и нормализован ($0)
- [x] Сравнительный анализ PROXY vs L1 OPT vs L1 SNAP
- [x] Gate 2 решение: жёлтый, cross-exchange в tail, пивот на funding + on-chain
- [x] Создан `docs/FUNDING_ARB_RESEARCH.md`
- [x] Создан `docs/ONCHAIN_ARB_RESEARCH.md`

### 2026-04-16 — Gate 1 cross-exchange arb (Этап 1)
- [x] Смена методологии с L1 bookTicker на trades-based proxy (после probe источников)
- [x] Даунлоадеры Binance aggTrades / Bybit spot / OKX trades
- [x] Нормализация trades к единой per-second схеме с bid/ask proxy
- [x] Детектор арб-окон с forward-fill staleness cap 30s
- [x] Результаты: BTC 138 окон/мес, ETH 563 окон/мес за март 2026
- [x] Gate 1 решение: зелёный на Этап 2 (Tardis валидация)
- [x] Оценка стоимости добавления пары (до 50 пар в пределах ноутбука)
- [x] On-chain research добавлен в Backlog

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
