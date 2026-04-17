# План

## In Progress

### Miro Strategy: Breakout + Retest — развитие и production

**Единственный трек показавший alpha.** Первый backtest: 5/8 монет в плюсе,
BTC +14.1% (42% win rate), на медвежьем рынке. R:R 1:3 работает.

**Этап 1 — Backtest + ML (DONE)**
- [x] Базовый backtest v1: level detection + breakout + retest (8 монет, 6 мес, 4h)
- [x] v2: trend filter + out-of-sample split → 2.4% годовых (честный)
- [x] v3: rolling levels walk-forward, закол → -6.6% годовых (автомат убыточен)
- [x] ML classifier: GradientBoosting 29 features → WR 23%→32.5%, +6.6% годовых
- [x] Top features: volume_ratio, volume_trend, abs_move_30d = "монеты в игре"

**Этап 2 — Screener (ТЕКУЩИЙ, параллельно с Этапом 3)**
- [ ] Screener: сканер 50+ монет, rolling level detection + breakout/retest/закол
- [ ] "Монеты в игре" detector: CoinGecko trending API + volume spike (CCXT)
- [ ] CryptoPanic news count как доп. сигнал
- [ ] Telegram bot: alert с графиком при формировании паттерна
- [ ] ML score в alert (GradientBoosting, P(win))
- [ ] Paper trading: ручные решения по screener alerts, 2-4 недели
- [ ] **Gate strategy-2:** manual WR >35% на 30+ сделках?

**Этап 3 — Формализация "eye test" (параллельно с Этапом 2)**

Цель: автоматизировать то, что человек видит глазами.
5 конкретных направлений, каждое добавляет ~2-3 п.п. к WR:

- [ ] **Per-touch level quality** — для каждого касания уровня считать:
      volume на касании, длина тени, скорость отскока (candles to reverse),
      расстояние между касаниями. Агрегировать в level_quality_score.
- [ ] **Swing structure** — HH/HL/LH/LL sequence detector.
      Uptrend = HH+HL, downtrend = LH+LL. Торговать только в direction структуры.
- [ ] **Multi-TF analysis** — уровни на D1, паттерн на H4, precision entry на H1.
      Уровень виден на D1 = сильнее чем виден только на H4.
- [ ] **Coin-in-play scoring** — volume_ratio + abs_move_7d + CoinGecko trending +
      news mentions. Composite score, отсечка: торговать только top-20%.
- [ ] **Claude Vision API experiment** — скормить скриншот графика, спросить
      "это хороший сетап для long?". Проверить на 50-100 примерах.
      Стоимость: ~$0.5-2 за запрос (vision). Тест: ~$50-100.

**Target:** каждое направление +2-3 п.п. WR. Суммарно 32% → 40-45% WR.
При R:R 1:3 и 40% WR = expectancy +1.6%/trade → **40-60% годовых**.

**Этап 4 — Live trading MVP**
- [ ] Execution через CCXT (spot, ордера limit)
- [ ] Risk manager: max 5% депо на сделку, дневной лимит убытков
- [ ] Hybrid mode: ML auto-trade (high confidence) + alerts (medium confidence)
- [ ] Начать с $1-2k, scale up при positive results

## TODO

### Phase 1: Core Infrastructure (продолжение)
- [ ] Реализовать WebSocket коннекторы для real-time данных
- [ ] Запустить и протестировать docker-compose (TimescaleDB + Redis)

### Phase 2: Data Collection
- [ ] Collectors для orderbook, trades, candles
- [ ] Исторические данные — загрузка и хранение
- [ ] Кэширование в Redis

### Phase 6: AI & Interface
- [ ] Claude Vision API для оценки сетапов (Этап 3)
- [ ] Claude Text API для market context summary
- [ ] FastAPI endpoints (screener API)
- [ ] Telegram бот (Этап 2 — alerts + trade management)

## Backlog

- Funding passive yield — 3.5-5% APR на idle capital (запустить когда есть капитал на биржах)
- Cross-exchange с colocation — ~20% APR, нужна инфра $200/мес (Phase 5, tail)
- ML модели для предсказания — после того как базовая стратегия работает
- Sentiment analysis (новости, соцсети)
- Web dashboard
- Triangular arb — вероятно мёртв как и остальной арб, low priority
- DEX интеграция — мёртв для арбитража (slippage), но может пригодиться для execution
- Rust core — отложен, bottleneck в стратегии а не в скорости

### Почему арбитраж отложен

По результатам исследования 16-17 апреля 2026 (6 треков, 21 скрипт, $0):
**чистый арбитраж — solved problem.** Подробности: `data/reports/arbitrage_final_report.html`

| Трек | Killshot | APR |
|---|---|---|
| Cross-exchange CEX | Окна в мс, L1 SNAP=0 | ~20% с colocation |
| Funding active | 0/114 profitable | negative |
| On-chain L1 | 87% окон короче блока | 0-10% |
| On-chain Arbitrum | Slippage $486 breakeven | ~0% |
| Alt DEX pools | Конкуренция или нет ликвидности | 0% |
| Small CEX | 0 окон, MMs everywhere | 0% |

### Почему Miro Strategy — main track

Первый backtest (без оптимизации) на 8 монет за 6 мес медвежьего рынка:

| Монета | Win% | Return | Max DD |
|---|---|---|---|
| BTC | 42% | +14.1% | -5.6% |
| DOGE | 35% | +14.1% | -7.7% |
| SOL | 31% | +9.7% | -12.7% |
| ETH | 30% | +9.4% | -20.1% |
| SUI | 30% | +8.8% | -25.1% |

5/8 монет в плюсе. При R:R 1:3 нужен 25% win rate для breakeven.
Реальная edge может быть 30-40% win rate = 10-50% годовых.

## Done

### 2026-04-17 — Miro Strategy backtest v1
- [x] Скачаны 4h свечи: BTC, ETH, SOL, LINK, ARB, PEPE, SUI, DOGE (6 мес)
- [x] Level detector: swing points + clustering (tolerance 1.5%)
- [x] Breakout detector: body close за уровнем
- [x] Retest detector: возврат к уровню + bounce
- [x] Backtest с R:R 1:3, SL 5% депо, fees 10 bps
- [x] Результат: 5/8 монет profitable, BTC best +14.1% (42% WR)

### 2026-04-17 — Slippage check + арбитраж финальный вердикт
- [x] Tick liquidity depth: Uniswap v3 WETH/USDC.e Arbitrum
- [x] Slippage при $500 = 2.7 bps (= median арб window), breakeven = $486
- [x] Slippage при $5k = 26.7 bps (10x median spread) — **DEAD**
- [x] Финальный HTML отчёт: `data/reports/arbitrage_final_report.html`
- [x] Вердикт: чистый арбитраж — solved problem, все 6 треков закрыты

### 2026-04-17 — Simple directional backtest
- [x] OHLCV 1h download (BTC + ETH, 6 мес)
- [x] Momentum, mean reversion, breakout, dual MA, buy-hold
- [x] Результат: все стратегии отрицательные, Sharpe < 0
- [x] Простые индикаторные стратегии не работают на текущем рынке

### 2026-04-17 — Niche arb: alt DEX pools + small CEX
- [x] C1: ARB/WETH, GMX/WETH, LINK/WETH на Arbitrum — конкуренция или нет ликвидности
- [x] C3: MEXC, Gate.io, Bitget vs Binance — 0 окон, MMs everywhere

### 2026-04-17 — On-chain CEX-DEX research Этап 1 (Gate onchain-1)
- [x] Pool state analysis L1 — staleness bias discovered (APR завышен 5-10x)
- [x] Execution timing filter — L1: 34 из 264 окон feasible (блок 12s)
- [x] Arbitrum: 9,030 свопов, 78 fresh окон/день, 70.7% закрыто ботами
- [x] Gate onchain-1: YELLOW → позже убит slippage check

### 2026-04-17 — Funding rate research Этап 1
- [x] 20 pairs × 3 exchanges × 12 мес
- [x] Gate funding-1: RED active, GREEN passive yield (3.5-5%)

### 2026-04-17 — Gate 2 cross-exchange arb
- [x] Tardis L1 validation: все окна ≤1 сек, L1 SNAP = 0 окон
- [x] Пивот на funding + on-chain

### 2026-04-16 — Gate 1 cross-exchange arb (Этап 1)
- [x] Trades-based proxy: BTC 138 окон/мес, ETH 563 окон/мес
- [x] Gate 1: зелёный на Этап 2

### 2025-02-22 — Phase 1: Core Infrastructure (часть 1)
- [x] Python проект, CCXT адаптеры, docker-compose, модели данных
