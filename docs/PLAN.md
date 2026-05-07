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
Четыре подтверждённые стратегии:

| Стратегия | Edge | WR | Annual (est.) | Sharpe | Allocation |
|---|---|---|---|---|---|
| **Weekend Ensemble** | **VOTE(BABA+NQ+XLK)→BTC** | **64%** | **+18% (18 t/yr)** | **2.82** | **30%** |
| Volume Ranking L/S | Volume momentum | — | +14% (Sharpe 1.6) | 1.6 | 30% |
| Miro + ML + Vision>=8 | S/R levels + AI filter | 58% | +7.3% (6 t/mo) | — | 25% |
| Funding capture >10bps, spread<5 | Structural exploit | 60-70% | ~+$5-14/мес@$25 (Bybit ceiling) | — | 15% |

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
- [ ] Weekend Ensemble Strategy:
      **VOTE(BABA fri + NASDAQ fri + Tech_sector week) → BTC weekend**
      Sharpe 2.82, WR 64%, avg +1.0%/trade, MaxDD -8.6%, profitable 6/6 years.
      91 трейдов за 5 лет (~18/год, каждый ~3й weekend).
      CONSISTENT half-split, recent (2024+) = +1.01%. Edge не decay'ится.
      
      **Риски:**
      - Overfitting: Sharpe 2.82 завышен — 64 комбинации протестированы, best-of-N bias.
        Реалистичная оценка: Sharpe 0.8-1.2, avg +0.4-0.6%/trade, ~8-12%/год.
      - Single-predictor decay: NASDAQ week slope -0.14%/yr (ensemble может замедлить, не устранить).
      - Weekend effect — известная тема, partially priced in.
      - Forward test 3 мес обязателен перед scale up.
      - Мониторить: 3 мес flat/negative → пересмотр.
      
      **Параметры:**
        - Signal: VOTE из 3 предикторов (BABA fri return, QQQ fri return, XLK week return)
        - Торгуем только при консенсусе 2/3 или 3/3
        - Entry: BTC Fri 21:00 UTC, direction = majority vote
        - Exit: Sun 23:00 UTC
        - SL: 2%
        - Опционально: ETH, SOL параллельно (диверсификация)
      
      Фаза 1 (ближайшая пятница): paper trade + Telegram alert
        - [ ] Скрипт: fetch QQQ/BABA/XLK, compute signals, send Telegram
        - [ ] Cron: запуск пятница 21:05 UTC
        - [ ] Ручное открытие позиции по alert
        - [ ] Закрытие вс 23:00 UTC
      Фаза 2 (4-8 нед paper): live на $500-1k без плеча
      Фаза 3: scale + leverage 2-3x если live Sharpe >1.0
- [ ] Risk manager: per-strategy allocation, conflict resolution, drawdown circuit breaker
- [ ] Live execution через CCXT (spot для Miro, futures для VR + funding)
- [ ] Начать с $2-5k, scale up при positive results

## TODO

### Next steps (после сессии 2026-05-05)

**Ближайшие (можно делать сейчас):**
- [x] **Funding spread filter: spread >= 5 bps → reject** — OOS-валидирован на 76 trades (split 38+38). TRAIN avg=+$0.04, TEST avg=+$0.035, WR=70%. Внедрён 2026-05-07.
- [ ] **Engine restart loop** — разобраться почему watchdog рестартует engine каждые 5 мин (8+ раз/сутки). Возможно WS disconnect → watchdog убивает → restart → repeat.

**Ждём данных:**
- [x] **Funding depth data collection** — ~~сейчас 29~~ → 76 trades с book_t2s (полные данные были в trades_log_server.json, не в CSV). Достаточно для анализа.
- [ ] **Screener signal collection** — с новым threshold (0.15/0.20) и breakout fix ждём signals + paper trades через Redis. Первый 4h scan 2026-05-05 16:00 UTC.
- [ ] **Volume Ranking paper data** — первый daily tick 2026-05-06 00:05 UTC. Через 30+ дней — анализ P&L.

**Scale-up — путь к $1k notional (Binance интеграция):**

Binance — главный блокер для scale. Fees 8 bps RT vs 11, книги 2.8x глубже (median), до 107x на отдельных монетах. При $1k: fit_L20=78% vs Bybit 8%.

**Шаг 1 — BinanceWebSocket** (`src/core/websocket/binance_ws.py`):
- [x] WS endpoints: public `wss://fstream.binance.com/stream`, private `wss://fstream.binance.com/ws/<listenKey>`
- [x] Auth: REST `POST /fapi/v1/listenKey`, keep-alive PUT каждые 30 мин
- [x] Public: `@markPrice@1s` -> PRICE_TICK + FUNDING_RATE events
- [x] Private: `ACCOUNT_UPDATE.FUNDING_FEE` -> транслируется в `execType=Funding` (executor не нужно менять)
- [x] Reconnect/heartbeat, 16 тестов

**Шаг 2 — Daemon refactor** (`src/engine/daemon.py`):
- [x] `--exchange binance` / `ENGINE_EXCHANGE=binance` env var
- [x] `_create_ws()` / `_create_ccxt()` factory functions
- [x] API keys: `{EXCHANGE}_API_KEY` env vars
- [x] Legacy compat: `bybit_api_key` still works, 15 тестов

**Шаг 3 — Strategy parametrize** (`src/strategies/funding_capture.py`):
- [x] REST scanner: dynamic `getattr(ccxt, exchange_id)` вместо hardcoded bybit
- [x] Binance: `fetch_funding_rates()` (funding не в tickers), 9 тестов
- [x] `category=linear` только для Bybit
- [x] Symbol format `BTC/USDT:USDT` совпадает на обеих биржах

**Шаг 4 — Executor**: не нужен — BinanceWebSocket транслирует events в Bybit-совместимый формат.

**Шаг 5 — Deploy**:
- [ ] Второй docker-compose service `engine-binance` с `ENGINE_EXCHANGE=binance`
- [ ] `.env`: добавить `BINANCE_API_KEY`, `BINANCE_API_SECRET`
- [ ] **Blocked: ждём Binance аккаунт + API key**

**После Binance:**
- [ ] **Depth filter при scale** — `effective/depth5 <= N` как hard filter (на $25 не работает, при $1k критичен)
- [ ] **Weekend signal -> live execution** — авто open/close BTC perp
- [ ] **Referral Rebate** — -30% к fees при scale up

**Отклонённые идеи (2026-05-05):**
- ~~Limit/maker exit~~ — fill rate 18%, экономия +1.4 bps/trade при adverse move -50 bps. Шум.
- ~~Maker entry~~ — ранний вход добавляет exposure (+8 сек drift risk), не окупает 8 bps fee saving.
- ~~Увеличение капитала до $100 на Bybit~~ — 83% трейдов превысят L1 depth (median $42). Масштабирование только через Binance.

### ~~Post-Settlement Continuation Dump~~ CLOSED
- [x] Проверено: continuation = зеркало counter-trade, та же directional bias. ALL FAIL с realistic costs.
- [x] "Sell to bots" (enter T-5m, exit T-1m): ALL FAIL — no pre-settlement anticipation drift.
- [x] Bot impact confirmed: +0.14% за T-1m candle (WR 68%), но < fee+slippage (0.26%). Untradeable без colocation.

### Colocation HFT feasibility estimate (research)
- [ ] **Посчитать ROI colocation для funding bot-front-run.** Данные: bot impact +0.14%/event, ~352 events/year (>50bps), 17 coins. Colocation ~$35k/year. Вопрос: при каком капитале colocation окупается? Учесть: (1) sub-second entry улучшает fill vs 1m candle, (2) реальный capture rate (не 100% events), (3) market impact при scale up, (4) сравнить с просто увеличением капитала в текущих стратегиях.

### Phase 1: Core Infrastructure (продолжение)
- [ ] Реализовать WebSocket коннекторы для real-time данных
- [ ] Запустить и протестировать docker-compose (TimescaleDB + Redis)

### Round-number level filter for Miro (LOW priority)
- [ ] **Добавить `is_round_number` вес в ML features.** Данные: 2209 round-number breaks vs 972 non-round. Round breaks: follow-through WR 51% (stable через 4h/8h/12h) vs non-round WR 43-47% (decays). Break size чуть меньше (1.09% vs 1.27%) — grid боты тормозят, но когда пробивает, continuation лучше. Реализация: добавить binary feature `level_is_round` в ML pipeline. Не отдельная стратегия, а фильтр.

### Phase 2: Data Collection
- [ ] Collectors для orderbook, trades, candles
- [ ] Исторические данные — загрузка и хранение
- [ ] Кэширование в Redis

### Phase 6: AI & Interface
- [ ] Claude Vision API для оценки сетапов (Этап 3)
- [ ] Claude Text API для market context summary
- [ ] FastAPI endpoints (screener API)
- [ ] Telegram бот (Этап 2 — alerts + trade management)

## TODO — Exploit Research

Фреймворк: "где деньги перемещаются предсказуемо?" Каждый exploit = gate research ($0), потом live тест если green.

### Tier 1 — низкий effort, проверяемо данными

- [x] **Basis Trade** — RED. APR 0.6-2% в bearish рынке. Работает только в contango (bullish). Пересмотреть при смене рыночного режима.
- [x] **Token Unlock Dumps** — YELLOW. ARB стабильно -7...-15% после unlock. Но точные даты тр��буют платный API ($50-100/мес). Ручной research 1x/мес + шорт перед крупными unlocks — feasible.
- [ ] **Referral Rebate** — создать новый субаккаунт через свой реферал = -30% к fees. Сделать при scale up ($1k+ → $35/мес экономии).

### Tier 2 — нужен парсинг/инфра

- [x] **Launchpool Front-Run** — YELLOW. Edge вероятен, но volume spikes — late signal. Нужен парсер анонсов бирж (Telegram/RSS). Data inconclusive (bearish рынок заглушает signal).
- [x] **Liquidation Cascade Capture** — RED. Детектируемы (vol spike + price move), но не торгуемы: MFE +0.52% avg но reversal за 1-3 мин, все exit-стратегии убыточны (WR 33-56%, net negative). Cross-exchange propagation мгновенная (ms), нет arbitrage window. 9 trades за 7 дней — мало и шумно. Закрыто.
- [x] **Fee Rebate Mining (MM)** — ORANGE. Bybit: 121 пар со spread >4bps, est $10-15/day. Binance: rebate -2.5bps (profit on any fill). Но это отдельная MM система — high effort.

### Screener observability
- [x] **Breakout persistence fix** — breakout state теперь ремапится через candle_ts, переживает рестарт контейнера.
- [x] **ML threshold снижен** — 4h: 0.25→0.15, 1h: 0.40→0.20. Backtest: 200 signals/35d (WR 49%).
- [x] **Daily Digest** — cron 08:00 UTC → Telegram. Docker health, funding stats, screener scans, volume ranking.
- [x] **Volume Ranking paper mode** — enabled в engine, daily 00:05 UTC, targets в engine_state.db.
- [x] **Redis DNS fix** — Redis был запущен вне compose (сеть `bridge` вместо `trading_default`). Пересоздан через compose, скринеры подключились. Сигналы теперь публикуются в Redis.

### Data collection in progress

- [ ] **Orderbook T-2s snapshot analysis** — collecting book_t2s (bid1/ask1/bid5/ask5, spread) in trades_log metadata since 2026-04-24. 76 trades with book_t2s ready for analysis. Analyze: does book depth at T-2s correlate with net PnL / slippage? If yes, add as entry filter or adaptive qty sizing. **Данные:** сервер `<SERVER_HOST>:/root/trading/data/engine_state.db` → таблица `trades_log`, поле `metadata.book_t2s`. Локальная копия: `data/trades_log_server.json`.

### Tier 3 — next up after funding capture stabilization

- [ ] **Funding Dump Trade** — short 5 min after settlement. Research: 100% dump rate at 5 min, median -0.58%, WR 57%, EV +0.179%/trade. Sim on 2026-04-23 data: +$9.29/day on $1k (+18% on top of base capture). Reuses same WS subs, no extra infra. StdDev 1.06% — noisy, needs 50+ trade sample to validate.
- [ ] **Funding Spot Hedge** — buy spot + short perp, collect funding, close both. Delta neutral = zero price risk. Breakeven at >31bps (spot fee 0.1% + perp 0.055% = 31bps RT). Research: 100% WR at >31bps threshold, 1.4 events/day. Sim on 2026-04-23: +$6.56/day on $1k (6 eligible coins). Needs spot market availability check.
- [ ] **Weekend Effect** — funding rates extreme in weekends? Check history by day of week.
- [ ] **Cross-TF Funding** — 4h/1h coins give 2-6x more settlements. Already partially doing.

## TODO — Multi-Exchange Expansion

Scale funding capture to multiple exchanges — different liquidity pools, no cross-crowding. Add after Bybit engine is stable + dump/hedge layers validated.

- [ ] **Binance funding capture** — fees 8bps vs 11bps Bybit. Maker rebate -2.5bps → breakeven 1.5bps. 15-65x deeper books. Different coin set = more opportunities without increasing per-exchange impact.
- [ ] **OKX funding capture** — similar fee structure. Third independent pool.
- [ ] Adapt engine for multi-exchange (exchange factory in daemon.py, per-exchange WS)
- [ ] Split capital: Binance 50% + Bybit 40% + OKX 10%

## Backlog — Structural Exploits

- **Token Unlock Shorts** — ARB стабильно -7...-15% после unlock за 3 месяца подряд. Предсказуемый механический поток (VC/team продают). Нужно: TokenUnlocks.app API ($50-100/мес) для точных дат + % supply. Ручной research 1x/мес viable. При $1k позиции: $70-150 за event. **Приоритет: HIGH при scale up.**
- **Launchpool Front-Run** — long staking token (BNB/MNT) при анонсе launchpool. Buying pressure предсказуем. Нужно: парсер Binance/Bybit announcements (Telegram каналы, RSS). Effort: MEDIUM. **Приоритет: MEDIUM.**
- **Binance Maker Rebate MM** — Binance платит -0.025% за maker fills. Любой fill = profit. Нужно: MM бот (order management, inventory risk). $10-50/day на $1-5k. Effort: HIGH (отдельная система). **Приоритет: LOW.**

## Backlog — Orderbook Microstructure

### Orderbook-based стратегии (L2 depth analysis)

**Контекст:** скальперы торгуют пробои после размытия айсбергов — крупный скрытый ордер держит уровень, после исчерпания цена пробивает. Citadel/Virtu/Jump строят на этом целый бизнес. Академически доказано: order flow imbalance предсказывает движение цены на 1-5 сек (Cont et al., 2014, r=0.3-0.5).

**Проблема:** требует колокация (microsecond latency) + капитал. С VPS + 50ms latency + $1k — не конкуренты HFT.

**Реалистичный путь:**
1. **Фаза 0 — Research (текущая):** собрать L2 data через Bybit WS `orderbook.50`, 5-10 монет, 2-3 дня. Искать: iceberg patterns, order flow imbalance, depth-price correlation. Цена: $0 (только сервер).
2. **Фаза 1 — Execution optimization:** использовать L2 data для улучшения entry/exit в funding capture. Limit order placement на уровнях с поддержкой в стакане. Ожидание: -3-5 bps slippage. Не требует HFT infra.
3. **Фаза 2 — Standalone стратегия (если Фаза 0 покажет edge):** iceberg detection + breakout. Нужно: доказать edge на исторических данных → тогда обосновать колокацию и капитал.

**Data sources:**
- Bybit WS `orderbook.50` — бесплатно, 50 уровней, 20ms updates
- Tardis L2 paid — $50-500/мес за исторические snapshots
- Binance data.vision — бесплатный L2 архив (громоздко)
- Свой коллектор — 2-4 недели разработки

**Критерий запуска Фазы 1:** funding capture стабильно прибылен 30+ дней.
**Критерий запуска Фазы 2:** доказанный edge на 1000+ L2 событий с положительным EV.

**Приоритет: LOW (research mode). Держим в голове: если найдём edge — колокация и капитал найдутся.**

## Backlog — Weekend Signal Improvements

- **Position scaling по consensus**: 1x при 3/5 majority, 1.5x при 5/5 unanimous. OOS данные: 5/5 WR 78%, avg_net +1.47% vs 3/5 WR 64%, avg_net +1.17%. N=9 на H2 — мало, но consistent с H1 (70%, +2.0%). Реализовать в `src/weekend/runner.py` → `config.py` добавить `unanimous_scale_factor`.

## Backlog — Bot Exploits

- **Post-Listing Dump Short** — шорт micro-cap через 10 мин после Bybit листинга. 13 листингов за 7 мес: avg spike +12%, short@10min→30min avg +2.5%. Проблемы: N=13 (мало), нужен парсер анонсов (Telegram/RSS), шорт может быть заблокирован >10 мин, WR подсчёт сомнительный. Частота ~22/год. **Приоритет: LOW.** Ждём парсер анонсов + больше данных.
- **MM Spread Exploitation** — когда ММ уходят перед settlement (spread 5-25bps vs нормальные 1-2bps), ставить limit orders в расширенный спред как temporary MM. Profit = spread - fees если обе стороны fill. Risk: one-sided fill. Нужно: real-time spread monitoring через WS, cancel logic после settlement. **Приоритет: LOW.** Требует orderbook WS инфры.
- **Colocation HFT for funding bot front-run** — bot impact +0.14%/event при >50bps funding, 352 events/year. Colocation ~$35k/yr. Breakeven ~$71k notional/trade. **Приоритет: VERY LOW.** Требует ~$100k капитала для окупаемости.

## Backlog — Прочее

- Funding passive yield — 3.5-5% APR на idle capital
- ML модели для предсказания — после того как базовая стратегия работает
- Sentiment analysis (новости, соцсети)
- Web dashboard
- Rust core — отложен, bottleneck в стратегии а не в скорости
- On-chain analytics — whale tracking, exchange flows, DEX volume
- Copy trading / signal aggregation
- **Tokenized stocks (xStocks) weekend monitoring** — xStocks (Backed Finance) торгуются 24/7 на Solana/Kraken. Когда ликвидность вырастет: (1) собирать weekend premium/discount xSPY/xQQQ vs Friday close как real-time sentiment предиктор для BTC weekend, (2) мониторить weekend spread/depth как ранний сигнал умирания нашего weekend edge (если spread сужается — 24/7 equity price discovery убивает lag), (3) whale wallet tracking on-chain для "informed weekend trading". Данные: Kraken API (бесплатный, public endpoints), Bitquery GraphQL (Solana on-chain), StocksOnSolana.com. NB: айсберги NYSE через xStocks не видны — orderbooks раздельные, информация течёт NYSE->xStocks. **Приоритет: LOW.** Ждём пока xStocks weekend volume станет значимым.
- **P2P premium как capital flight indicator** — гипотеза: санкции/война → capital flight через крипту → premium на P2P (рубли, лиры, риалы) растёт → BTC buying pressure. Проверка: собирать Binance P2P API цены, смотреть premium vs global price как leading indicator. Проблемы: исторических данных нет (начать собирать), N событий мало (5-10 войн/санкций за 5 лет), capital flight маскируется шумом (0.5-2% от daily volume), war = risk-off перебивает capital flight (февраль 2022 BTC -20%). **Приоритет: LOW.** Нужно 6-12 мес сбора данных прежде чем тестировать.

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
