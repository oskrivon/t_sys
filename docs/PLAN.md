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

**Этап 2 — Screener (параллельно с Этапом 3)**
- [x] Screener: сканер 50+ монет, rolling level detection + breakout/retest/закол
- [x] "Монеты в игре" detector: volume spike (CCXT) + big movers
- [ ] CryptoPanic news count как доп. сигнал
- [x] Telegram bot: alert с графиком при формировании паттерна
- [x] ML score в alert (GradientBoosting, P(win))
- [x] Paper trading инфраструктура: SQLite БД + авто-трекинг TP/SL + CLI stats
- [ ] Paper trading: ручные решения по screener alerts, 2-4 недели
- [ ] **Gate strategy-2:** manual WR >35% на 30+ сделках?

**Этап 3 — Формализация "eye test" (параллельно с Этапом 2)**

Цель: автоматизировать то, что человек видит глазами.

- [x] **Per-touch level quality** — 22 фичи: volume ratio/trend/max, wick rejection,
      bounce speed, touch gap, zone tightness. На 4H и D1.
      WR +7.4pp (38.8->46.2%), PF 2.12->3.91, exp x2. С risk 4% = **14.3% annual (best).**
- [x] **Swing structure** — HH/HL/LH/LL detector + 8 фичей. 5.7% importance,
      но annual не растёт (+13.4% vs 14.3% baseline). Полезно как фича, не как фильтр.
- [x] **Multi-TF analysis** — уровни на D1, вход на 4H.
      D1 уровни дают +20pp к WR (24%→44%) и PF 0.85→2.61.
      **Главный прорыв: 4H/d1_only + ML = 13.3% годовых.**
- [x] **Coin-in-play scoring** — volume_ratio + abs_move_7d + range_expansion.
      cip_range_expansion = 3.7% importance, WR +3pp, но annual +14.2% (= baseline).
      Полезно как ML фича, не как hard filter.
- [x] **Claude Vision API experiment** — протестировано на 4H и 1H.
      sonnet-4.6/binary лучший (corr +0.208), но ML сильнее.
      Vision = слабый доп. сигнал, не основной фильтр.

**Target:** каждое направление +2-3 п.п. WR. Суммарно 32% → 40-45% WR.
При R:R 1:3 и 40% WR = expectancy +1.6%/trade → **40-60% годовых**.

**Текущий лучший результат (honest walk-forward OOS):**
ML top-10 + **Vision score>=8** = **WR 57.8%, PF 3.46, ~6 trades/mo.**
407 OOS trades, 51 символ, 24 мес. Vision стоит $0.60/мес.
(ML-only = WR 31%, PF 1.09. Vision>=8 = главный фильтр.)

**Этап 3.5 — Reverse Pattern Discovery + совмещение**

Data-driven подход: анализ что предшествует сильным движениям (>5% за 6h).
Обнаружена предсказуемость 80% precision при thr 0.7. Ключевые предикторы:
volatility squeeze, volume spike, wicks, hour_utc, RSI.

- [x] **Бэктест "Big Move Detector" стратегии** — standalone убыточен (WR 36%, PF 0.79).
      Причина: предсказывает timing, но не direction. RSI — плохой direction picker.
- [x] **Совмещение с Miro:** WR 44→48%, PF 2.6→4.5, но трейдов 19→4/мес.
      Annual 13.3% → 4.7%. Двойной фильтр избыточен — Miro уже ловит движения.
- [ ] **Squeeze screener** — отдельный alert: "волатильность сжалась + объём растёт,
      скоро будет движение >5%". Без direction = для straddle или ожидания.
- [x] **Hourly bias** — hr_europe marginal +1.1pp (15.4% vs 14.3%). Самый слабый эффект.

**Этап 3.6 — Volume Ranking Long/Short (новый трек)**

Подтверждено: Sharpe 1.62 net, +14.2% annual, MaxDD 6.4%. Market-neutral.
Некоррелирован с Miro (разный edge: volume momentum vs S/R levels).

- [ ] Paper trading: daily rebalance, track PnL в SQLite
- [ ] Интеграция в multi-strategy portfolio manager
- [ ] Bybit/Binance futures execution (maker orders для снижения fees)

**Этап 4 — Live trading MVP (multi-strategy)**

Архитектура построена: Strategy ABC + PortfolioManager + ExecutionManager.
Три подтверждённые стратегии:

| Стратегия | Edge | WR | Annual (est.) | Allocation |
|---|---|---|---|---|
| Miro + ML + Vision>=8 | S/R levels + AI filter | 58% | +7.3% (6 t/mo) | 40% |
| Volume Ranking L/S | Volume momentum | — | +14% (Sharpe 1.6) | 50% |
| Funding capture >10bps | Structural exploit | 56% | +192% (tiny size) | 10% |

- [ ] Paper trading validation: 4 недели все 3 стратегии параллельно
- [ ] Vision интеграция в live screener (score>=8 → trade, <5 → skip)
- [ ] Volume Ranking daily rebalance бот (futures, maker orders)
- [x] Funding capture бот:
      Фаза 1: ✅ WS мониторинг 573 пар, dynamic watchlist, entry/exit automation
      Фаза 1.5: ✅ Первый live тест 12:00 UTC — 9 позиций, баги найдены и пофикшены
      Фаза 2 (текущая): micro-live $15, отладка exit timing, сбор статистики
      Фаза 3: scale up до 10-20 монет, 10x, target $50-100/day
- [x] Trading Platform architecture:
      ✅ Redis pub/sub (5 каналов), Signal schemas (Pydantic)
      ✅ Paper Trading Service (standalone, Redis subscriber)
      ✅ Screener decoupled (Redis publish, PaperTrader fallback)
      ✅ Telegram Bot (commands + Redis forwarding)
      ✅ Engine Redis integration (commands, events, status KV)
      ✅ Strategy config from YAML (config/strategies.yml)
      ✅ Docker Compose (6 сервисов)
- [ ] Risk manager: per-strategy allocation, conflict resolution, drawdown circuit breaker
- [ ] Live execution через CCXT (spot для Miro, futures для VR + funding)
- [ ] Начать с $2-5k, scale up при positive results

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

- Funding dump trade — short 5 мин после settlement: +0.15% net, WR 57%. Спички на $15, $315/мес на $1k. Реализовать при scale up.
- Funding spot hedge — buy spot + short perp при >31bps: 100% WR, $15/мес на $100. Для крупных позиций на volatile coins.
- Funding passive yield — 3.5-5% APR на idle capital (запустить когда есть капитал на биржах)
- Cross-exchange с colocation — ~20% APR, нужна инфра $200/мес (Phase 5, tail)
- ML модели для предсказания — после того как базовая стратегия работает
- Sentiment analysis (новости, соцсети)
- Web dashboard
- Triangular arb — вероятно мёртв как и остальной арб, low priority
- DEX интеграция — мёртв для арбитража (slippage), но может пригодиться для execution
- Rust core — отложен, bottleneck в стратегии а не в скорости
- On-chain analytics — whale tracking, exchange flows, DEX volume. Другой тип edge (информационный), не совместим с текущей level-based стратегией. Требует Nansen/Glassnode ($100-300/мес) + новый стек. Рассматривать после live trading.
- Copy trading / signal aggregation — не слепое копирование, а использование чужих сигналов как input для ML. Подписка на 2-3 копитрейдера (Bitget/Bybit) + проверка через наш ML/levels = двойное подтверждение. Нет исторических данных — только forward test. Лидерборд API закрыты.
- Funding scalp (базовый) — DEAD для average funding. Но HF capture с leverage + extreme filter (>10bps) = +$160/мес на $1k. Реализовано в Этапе 4.
- Exploit research — систематический поиск structural edges: token unlocks, liquidation cascades, basis trade, launchpool farming. Framework: "где деньги перемещаются предсказуемо?"

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
