# Прогресс

## Лог

### 2026-04-18 — Claude Vision test: ПОДТВЕРЖДЕНО на 100 примерах

**Claude Sonnet 4.6 через OpenRouter оценивает сетапы по скриншоту графика.**

**20 примеров (pilot):** correlation +0.595, score>=7 → 100% WR (7/7)

**100 примеров (validation):**
- Avg score: wins **6.4**, losses **5.0**, delta **+1.3**
- Correlation **+0.429** (стабильно сильная)
- Score >= 6: 68 trades, **63% WR**
- Score >= 7: **36 trades, 75% WR** ← ключевая метрика
- Score >= 8: 7 trades, 100% WR
- Latency: **2.5-4.3 сек/запрос** — некритично для 4h
- Cost: **$0.004/запрос**, total test ~$0.40
- **При R:R 1:3 и 75% WR: expectancy +2R/trade → ~100% годовых (теор.)**
- **Консервативно (50-60% WR live): 30-60% годовых**

### 2026-04-18 — Claude Vision test: скрипт готов, ждёт API key fix

- Скрипт `claude_vision_test.py`: генерирует candlestick chart с уровнями → Claude Sonnet оценивает сетап 1-10
- OpenRouter API: 401 "User not found" — нужно проверить ключ/баланс
- Estimated cost: ~$0.20 за 20 примеров, ~$5 за 500
- **Когда заработает:** запустить `python scripts/research/claude_vision_test.py --n 20`

### 2026-04-18 — ML classifier для Miro Strategy

- GradientBoosting на 1,218 trades (29 features, 5-fold CV)
- Baseline WR 23% → filtered WR **32.5%** (threshold 0.35, отсеивает 81% сигналов)
- **Top features: volume_ratio, volume_trend, abs_move_30d** — ML сам нашёл "монеты в игре"
- Expectancy: -0.17%/trade → **+0.52%/trade**
- Annual estimate: -6.6% → **+6.6%** (base), +11.2% (optimistic)
- BTC/ETH/LINK плохо работают (14%, 14%, 12% WR), AVAX/NEAR/DOGE лучшие (47%, 43%, 39%)
- Вывод: ML помогает, но фундаментально ограничен качеством level detector

### 2026-04-18 — Miro Strategy v3: rolling levels walk-forward

- Rolling level detection (lookback 200, update каждые 6 свечей) — no look-ahead bias
- 12 монет, 7-8 мес, walk-forward: 739 trades, 24.9% WR, PF 0.84
- Закол = 86% сигналов (слишком шумный), retest = 14%
- Best: NEAR +39.5% (PF 1.65), SUI +19.8% (PF 1.41)
- Worst: BTC -35.3% (PF 0.25), DOGE -30.1% (PF 0.54)
- Автоматическая стратегия в чистом виде убыточна (-6.6% годовых)

### 2026-04-17 — Miro Strategy backtest v1: ПЕРВЫЙ ПОЛОЖИТЕЛЬНЫЙ РЕЗУЛЬТАТ

**Стратегия из Miro:** пробой + ретест горизонтальных уровней S/R.
R:R 1:3, SL 5% депо, 4h TF, 8 монет, 6 мес (медвежий рынок).

**Retest (основной паттерн):**
- BTC: 12 trades, 42% WR, **+14.1%**, DD -5.6%
- DOGE: 23 trades, 35% WR, **+14.1%**, DD -7.7%
- SOL: 29 trades, 31% WR, **+9.7%**, DD -12.7%
- ETH: 30 trades, 30% WR, **+9.4%**, DD -20.1%
- SUI: 43 trades, 30% WR, **+8.8%**, DD -25.1%
- LINK: 22 trades, 23% WR, -1.3% (breakeven)
- ARB: 28 trades, 14% WR, -24.3% (bad)
- PEPE: 41 trades, 15% WR, -25.2% (bad)

**5/8 монет в плюсе на медвежьем рынке** (BTC -30%, ETH -40% за период).
Это первый backtest без оптимизации. Потенциал для улучшения: фильтр тренда,
закол, volume filter.

### 2026-04-17 — Slippage check: on-chain арбитраж окончательно мёртв

- Liquidity check Uniswap v3 WETH/USDC.e Arbitrum (live pool state)
- Slippage $500 = 2.7 bps (= median arb window), breakeven $486
- Slippage $5k = 26.7 bps — в 10x больше median spread
- **On-chain арбитраж structural dead** — пул слишком тонкий
- Финальный HTML отчёт: `data/reports/arbitrage_final_report.html`

### 2026-04-17 — Simple directional: индикаторы не работают

- 5 стратегий (momentum, mean-revert, breakout, dual MA) на BTC+ETH 1h
- Все отрицательные, Sharpe < 0, ни одна не бьёт buy-hold
- Подтверждение: простые сигналы не дают alpha. Нужны паттерны из price action.

### 2026-04-17 — Niche arb research: alt DEX pools + small CEX

**C1: Alt pools on Arbitrum (ARB/WETH, GMX/WETH, LINK/WETH):**
- ARB/WETH 0.05%: 10,043 свопов, gap median 5s — **арбитражится как WETH/USDC**
- GMX/WETH 0.3%: 232 свопа, gap median 124s — мало конкуренции, но 30bps fee + нет ликвидности
- LINK/WETH 0.3%: 105 свопов, gap 51s — пул почти мёртв
- **Вывод:** alt pools either too competitive (0.05%) or too illiquid (0.3%)

**C3: Мелкие CEX (MEXC, Gate.io, Bitget) vs Binance:**
- Real-time данные за 2 часа, ETH/USDT + BTC/USDT
- **0 арб-окон** при 20 bps threshold
- MMs арбитражят даже мелкие биржи на top-парах

**Общий вывод:** чистый арбитраж — solved problem. Все ниши заняты или неликвидны.

### 2026-04-17 — On-chain CEX-DEX: Pool state analysis + Arbitrum (Gate onchain-1)

**Pool state analysis (L1 Ethereum):**
- Построен per-second timeline цены пула между свопами vs CEX mid
- Staleness effect discovered: >300s без свопов → spread 92-166 bps (стеклянный пул)
- Без freshness filter APR завышен в 5-10x. Реально на L1: 264 fresh окна, median 3s
- Execution timing: L1 блок 12s → только 34 окна (12.9%) физически исполнимы
- L1 realistic APR: ~123% при $10k, 20% capture

**Arbitrum (реальные данные):**
- 9,030 свопов WETH/USDC.e за 2026-03-01 (2.3x больше чем L1)
- Оптимизация: sparse timestamp sampling (102 RPC calls вместо 6,566)
- Median gap: 3s (vs 24s на L1, 8x быстрее корректировка)
- 78 fresh окон/день (vs 264 L1), но все исполнимы (block 0.25s)
- 70.7% окон закрыто swap-ом (больше конкуренция чем на L1)
- Arbitrum APR: ~241% при $10k, 20% capture

**Gate onchain-1: YELLOW**
- Arbitrum — лучший трек из всех: APR 120-360% при $5-10k
- Для сравнения: funding 4%, cross-exchange 20%
- Переход к Этап 2: EOA prototype на Arbitrum

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
