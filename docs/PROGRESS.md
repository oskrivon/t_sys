# Прогресс

## Лог

### 2026-04-17 — Funding rate research Этап 1
- Downloaded funding history: 20 pairs × 3 exchanges (Binance/Bybit 12 мес, OKX 3 мес)
- Analysis: mean APR +3.5% BTC, +3.0% ETH, best LINK +4.9%. 40% pairs negative.
- Backtest v1 (threshold): all negative at 20-24 bps roundtrip fees
- Backtest v2: buy-and-hold 35/58 profitable (3.5-5% APR), cross-exchange 0/114 profitable
- Gate funding-1: RED for active, GREEN for passive yield. Pivot to on-chain.

**On-chain CEX-DEX preliminary research (тот же день):**
- Скачано 8,080 Uniswap v3 WETH/USDC свопов за 2026-03-01 через free public RPC
- Spread vs Binance: 15.8% свопов profitable после 15bps fees (median 10.3 bps)
- На Arbitrum ($0.10 gas): 1,176 profitable свопов при $1k trade size
- Caveat: execution price ≠ opportunity — часть свопов = уже захваченный арб
- **Strongest signal across all research tracks** — продолжаем в следующей сессии

### 2026-04-17 — Gate 2 cross-exchange arb: пивот на funding + on-chain
- Скачан free Tardis sample за 2026-03-01 (book_ticker L1, 6 файлов, ~270MB, $0)
- Написаны `download_tardis.py`, `normalize_tardis.py`, `compare_proxy_vs_l1.py`
- Сравнение PROXY vs L1 OPT vs L1 SNAP:
  - BTC: 14/15/**0** окон. L1 OPT precision 93%, recall 93% против proxy
  - ETH: 41/47/**0** окон. L1 OPT precision 85%, recall 73% против proxy
  - **Все окна на L1 — 1 секунда или меньше**. L1 SNAP (раз в секунду polling) — **0 окон**
- **Ключевой вывод:** cross-exchange окна существуют только в sub-секундном масштабе. HFT/MMs закрывают их внутри секунды.
- Разбор инфраструктуры: Rust не решает (проблема в сети, не CPU). Colocation VPS в регионе биржи — правильный подход, latency 20-50ms. С учётом конкуренции с market makers реалистичный fill rate 20-40%, APR ~20% на $10k.
- **Gate 2 решение: жёлтый, переприоритизация:**
  - Main track: funding rate arb (APR 8-30%, без HFT-конкуренции, Python на равных)
  - Parallel: on-chain CEX-DEX research без смарт-контрактов (The Graph + EOA swaps)
  - Side: triangular research на альткоинах
  - Tail: cross-exchange WS + colocation — после остальной инфры
- Созданы `docs/FUNDING_ARB_RESEARCH.md` и `docs/ONCHAIN_ARB_RESEARCH.md`
- `docs/PLAN.md` переприоритизирован

### 2026-04-16 — Arbitrage Research Этап 1 (trades-based proxy)
- Пройден probe источников: L1 bookTicker недоступен бесплатно ни у Binance, ни у Bybit, ни у OKX (ни архивы, ни REST API). On-chain тоже не помогает (CEX-сделки off-chain).
- Сменили методологию: trades-based bid/ask proxy через `isBuyerMaker`/`side`.
- Написаны даунлоадеры: `download_binance_trades.py`, `download_bybit_trades.py`, `download_okx_trades.py`.
- Скачано за март 2026: Binance (1013MB) + Bybit (403MB) + OKX (298MB) = 1.7 GB для BTC+ETH / 3 биржи.
- Написан `normalize_trades.py` — chunked pandas, per-second агрегат → parquet.
- Написан `arbitrage_detect.py` — детектор окон с forward-fill 30s, комиссии 20bps round-trip.
- **Результаты:** BTC 138 окон/мес (медиана 1с, net_bps 4.0), ETH 563 окон/мес (медиана 1с, net_bps 4.8).
- **Gate 1 пройден (зелёный):** идём на Этап 2 (Tardis L1 sample, до $50).
- Добавлено в Backlog: on-chain research (exchange flows, CEX-DEX arb).

### 2025-02-22
- Создана начальная структура проекта по шаблону
- Проведено исследование: существующие платформы, стек для low-latency
- Определены приоритетные биржи: Binance, Bybit, OKX
- Выбран стек: Python (asyncio) + опционально Rust для HFT
- Решение: начинаем с Python, Rust добавляем если упираемся в латентность
- Создана документация по настройке API ключей бирж (docs/EXCHANGES_SETUP.md)
- Реализован Settings/Config с pydantic-settings
- Реализованы базовые модели: Ticker, Candle, Order, Trade, Balance, Position, OrderBook
- Реализован CCXTAdapter — универсальный адаптер для бирж через CCXT
- Реализован ExchangeManager — менеджер для работы с несколькими биржами
- Создан тестовый скрипт scripts/test_connection.py

## Бенчмарки

### Базовые метрики

| Метрика | Значение | Дата | Контекст |
|---------|----------|------|----------|
| Python обработка тика | ~250-500 μs | 2025-02-22 | Референс из исследования |
| Rust обработка тика | ~6-12 μs | 2025-02-22 | Референс из исследования |
| Binance API latency | ~50-100 ms | 2025-02-22 | Референс |
| Binance WS latency | ~10-30 ms | 2025-02-22 | Референс |

### История измерений

<!-- Шаблон:
### YYYY-MM-DD — [Что изменилось]
**До:** ...
**После:** ...
**Вывод:** ...
-->
