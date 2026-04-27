# Прогресс

## Лог

### 2026-04-27 — Weekend Ensemble: VOTE(BABA+NQ+Tech) Sharpe 2.82 (BEST STRATEGY FOUND)

**Поиск предикторов:** 32 тикера × 2 сигнала = 64 комбинации протестировано.

**Сюрпризы:**
- **Alibaba fri** (#1 single, Sharpe 1.38) — бьёт NASDAQ (1.10). Азиатский risk sentiment.
- **Japan fri** (#2, Sharpe 1.32) — самый сильный recent (+0.62%). Японский ритейл активен на выходных.
- **China Internet fri** (#4, Sharpe 1.26) — улучшается во второй половине выборки.
- Crypto-adjacent (MSTR, COIN) — посредственно (Sharpe 0.57). Слишком коррелированы с BTC.
- Oil, Gold, VIX — не работают или обратные.

**Ensemble VOTE — прорыв:**
- VOTE(BABA fri + NQ fri + Tech_sector week): **Sharpe 2.82**, WR 64%, avg +1.00%/trade
- 91 трейдов за 5 лет (~18/год), MaxDD -8.6%, **profitable 6/6 years** (включая 2023!)
- Recent (2024+) = +1.01% (edge НЕ decay'ится в ensemble)
- CONSISTENT half-split (H1: +1.26%, H2: +0.74%)
- Vs baseline NASDAQ week (Sharpe 1.10): improvement **2.6x**

**Почему ensemble лучше:** торгует только когда 2+ из 3 independent предикторов согласны. Фильтрует шумные сигналы → WR 64% vs 55%, MaxDD -8.6% vs -20.7%.

### 2026-04-27 — Weekend Effect: full deep dive, 3 assets, 5 years (CONFIRMED)

**Deep dive on best strategy: NASDAQ week_ret → crypto weekend (Fri 21:00 → Sun 23:00)**

**BTC (225 weekends, 2021-2026):**
- Overall: WR 55%, avg +0.43%, total +96%, **Sharpe 1.12**, MaxDD -21%. CONSISTENT.
- Year-by-year: positive every year except 2023 (flat). 2021: Sharpe 2.21, 2022: 1.56, 2024: 1.29.
- Signal strength sweet spot: 1.5-3% NQ move → WR 57%, avg +0.76%.
- Counter-trend (NQ signal vs BTC 30d trend) → WR 60%, avg +0.65% — лучше чем aligned.
- Optimal SL: 2% (31% stopped, total +81%). Without SL: total +96%.
- Exit timing: signal нарастает линейно Sat 00→Sun 23. Лучший exit Sun 23:00.

**ETH (225 weekends, 2021-2026):**
- Overall: WR 55%, avg +0.48%, total +107%, Sharpe 0.84, MaxDD -35%. CONSISTENT.
- Best in "down" regime (30d return -10% to -2%): WR 70%, avg +1.52%.
- 2023 убыточный (-8%), остальные годы в плюсе.

**SOL (185 weekends, 2022-2026):**
- Overall: WR 56%, avg +0.54%, total +99%, Sharpe 0.65, MaxDD -34%. CONSISTENT.
- Более волатильный (MAE -3.77%). Optimal SL = 3% (total +108%).
- Optimal exit раньше: Sun 12:00 (avg +0.96%) лучше чем Sun 23:00 (+0.54%).
- 2023 лучший год (Sharpe 1.13) — компенсирует BTC/ETH слабость.

**Caveat — overfitting risk:**
- Sharpe 2.82 завышен: 64 комбинации протестированы, выбрана лучшая (data mining bias)
- Annualization sqrt(52) при 18 trades/year некорректен
- Реалистичная оценка после correction: **Sharpe 0.8-1.2, avg +0.4-0.6%/trade, ~8-12%/год**
- Single predictor (NASDAQ week) без cherry-picking: Sharpe 1.1 — это floor
- Forward test 3 мес нужен для валидации реального edge

**Файлы:** `scripts/tmp/weekend_deep.py`, `weekend_deep_dive.py`, `weekend_eth_sol.py`, `data/reports/weekend_strategy_matrix.csv`

### 2026-04-27 — Weekend Effect: NASDAQ predicts crypto weekends (PROMISING)

**Гипотеза:** NASDAQ week return предсказывает направление BTC/ETH/SOL на выходных.

**Данные:** 226 выходных (Apr 2021 — Apr 2026), BTC/ETH/SOL 4h Bybit + QQQ/SPY/GLD/UUP daily. 72 комбинации (3 крипто × 4 equity × 3 предиктора × 2 exit).

**Лучшие стратегии:**
- BTC | NASDAQ week_ret → exit Sun 23:00: **Sharpe 1.12**, WR 55%, avg +0.43%/wk, total +98%, MaxDD -21%. **CONSISTENT** (H1: WR 55% avg +0.57%, H2: WR 56% avg +0.29%)
- BTC | NASDAQ week_ret → exit Mon 13:30: **Sharpe 1.21**, WR 54%, avg +0.70%/wk, total +157%, MaxDD -44%. CONSISTENT
- ETH | NASDAQ fri_ret → exit Sun 23:00: Sharpe 0.92, WR 54%, avg +0.51%/wk. CONSISTENT
- SOL | NASDAQ week_ret → Sun 23:00: Sharpe 0.68, WR 56%

**Не работает:** Gold, DXY как предикторы — Sharpe отрицательный. Эффект именно risk-on/risk-off (equity→crypto).

**Механизм:** NASDAQ week return = proxy для risk sentiment. Крипто-трейдеры на выходных реагируют на пятничный настрой рынка. К понедельнику эффект исчезает (institutional money возвращается).

**Статус: PROMISING — требует forward test и детальное копание.**

### 2026-04-27 — BTC-NASDAQ weekend gap hypothesis: REJECTED

**Гипотеза:** BTC и NASDAQ скоррелированы (r≈0.6). NASDAQ закрыт на выходных. При гэпе на открытии в понедельник BTC должен "подтянуться".

**Данные:** 49 выходных (May 2025 — Apr 2026), BTC 1h Bybit + QQQ daily.

**Результат:**
- NASDAQ gap → BTC weekend move: r=**+0.48** — BTC уже отыгрывает гэп ЗА ВЫХОДНЫЕ, до открытия NYSE
- NASDAQ gap → BTC Monday session: r=**-0.28** — обратная! BTC откатывает в понедельник (уже отыграл)
- Remaining gap → BTC Monday: r=-0.08 (ноль)
- Стратегия "trade remaining gap": WR 53%, avg return **-0.25%**, Sharpe **-1.14**, total **-12.4%/год**
- **Вердикт: REJECTED.** Рынок эффективен — BTC закрывает расхождение за выходные, к понедельнику ловить нечего.

### 2026-04-27 — Funding capture deep analysis: depth, sizing, post-funding, vision

**Данные:** 106 trades в engine DB (Apr 22-26), 102 entries в engine logs (Apr 20-26), 609 funding settlements (29 монет × 21 settlement за 7 дней).

**Результаты исследований:**

1. **Корреляция depth vs PnL (29 трейдов с book_t2s):**
   - spread_bps vs net_bps: r=-0.30, notional/top5 vs net_bps: r=-0.23
   - funding_bps vs pnl_pct: r=-0.80 (высокий фандинг = высокий slippage)
   - Порог: notional > 12% от top5 depth → средний результат убыточный

2. **Масштабирование до $1k (constant slippage model):**
   - Baseline slippage = -0.46% (settlement volatility, не market impact)
   - Breakeven funding rate = ~54 bps (slip 46 + fees 11 - 3 referral)
   - TOP-1 стратегия: +$64/день, но 3/7 дней убыточные (дни с funding < 50bps)
   - Depth-adaptive sizing: работает, но 94% капитала простаивает (стаканы мелкие)

3. **Referral -30% fee impact:**
   - Breakeven сдвигается с 57.1 до 53.8 bps (saving 3.3 bps/trade)
   - Fee = 19% затрат, slippage = 81% — referral не game changer
   - Лучший вариант: limit entry + referral → breakeven 51.4 bps

4. **Binance для scaling: НЕ приоритет.**
   - 13/20 top-funding монет — Bybit-only (KAT, MIRA, STABLE, NEWT, CHIP...)
   - Breakeven improvement: -1.4 bps (marginal)
   - Рекомендация: сначала доказать profitability на Bybit

5. **AI Vision на funding charts (37 трейдов scored):**
   - Корреляция score vs net_bps: **-0.17** (обратная!)
   - Модель видит volatility = "плохо", но для funding capture volatility = норма
   - Score 2-3 (самый "плохой"): avg net +39 bps. Score 6-7: avg net +1.5 bps
   - **Вердикт: vision для funding capture не работает в текущем виде**

6. **Post-funding bounce study (609 settlements × 29 coins):**
   - Средний post-10m move = **-0.01%** (ноль на большой выборке)
   - "Near-zero delta" гипотеза не подтвердилась
   - sett_delta → post_10m: r=+0.21 (continuation, не reversal)
   - Крупные bounces (+5-14%) = случайные pump'ы на illiquid альтах
   - **Вердикт: пост-funding trade не имеет edge**

**Файлы данных:**
- `data/trades_log_server.json` — 106 trades из engine DB
- `data/all_engine_funding_lines.txt` — 366 строк из всех engine логов
- `data/reports/post_funding_wide.csv` — 609 settlements wide study
- `data/reports/funding_vision_results.csv` — 37 vision scores
- `data/charts/funding/` — 50 1m графиков вокруг settlement
- `scripts/tmp/` — ~10 аналитических скриптов

### 2026-04-23 — Fix funding capture: precompute regression + min_qty bug

**Root cause:** commit `744a6b4` partially staged — executor fast-path (`_precomputed_qty`) was committed, but strategy producer side was left in stash. Result: bot traded min_qty ($0.03–$1.13) instead of $25 notional, and entered at T-10s instead of T-2s. 15 trades today, all losing, sum PnL -4.64%.

**Fixes:**
- Restored 3-phase `_schedule_entry`: T-10s precompute (set_leverage + compute qty) → T-2s fire order → T-0 settlement
- Restored `_precompute_entry()` method + `_target_notional` from config
- Signal metadata now carries `_precomputed_qty`, `_leverage_set`, `target_notional`
- Executor fallback logs `warning("precompute_missing_using_fallback")` instead of silent degradation
- Fixed symbol parsing: `.replace("USDT","")` → suffix strip (prevents USDT→empty for USDTUSDT-like symbols)

**Guard rails added:**
- Contract test: `assert "_precomputed_qty" in signal.metadata` in integration tests
- Architecture doc: "Cross-module contracts" section — 3 rules to prevent producer/consumer desync

### 2026-04-22 — Bugfixes + Universal Backtest Module + 7 Exploit Research

**Bugfixes deployed:**
- Engine: `set_leverage` clamp to exchange max (SIREN/XION ORDER FAILED fix)
- Screeners: `df.index` -> `df["ts"]` crash fix (both screeners dead 38h)
- CI/CD: screeners added to deploy, path-based filtering (engine not restarted on research changes)

**Universal Backtest Module (`src/backtest/`):**
- `models.py`: Trade, CostBreakdown dataclasses
- `cost.py`: pluggable pipeline (TakerFee, Slippage, Spread, FundingDuringHold, BorrowCost)
- `presets.py`: bybit_futures(), binance_futures() with real fee rates
- `metrics.py`: compute_metrics() -> WR, PF, Sharpe, DD, equity curve, cost breakdown
- `runner.py`: CandleStrategy, EventStrategy, PortfolioStrategy adapters
- Bybit futures total cost: ~16 bps/trade (5.5+5.5+2+1+1.8 funding)

**7 Funding Exploit Strategies — Research Results:**

All tested on 10 symbols, ~6 months, Bybit costs.

| # | Strategy | Best Config | N | WR | PF | Annual | Verdict |
|---|----------|-------------|---|----|----|--------|---------|
| S1 | Funding Prediction (2h before) | >2bps | 68 | 35% | 0.44 | -59% | RED |
| S2 | Funding Dump (after settlement) | >3bps/60m | 27 | 44% | 0.92 | -3% | RED |
| S3 | Spot Hedge (delta-neutral) | >2bps | 0 | - | - | - | RED (no events) |
| S4 | ADL Front-Run | >2bps/3x | 6 | 50% | 1.05 | +2% | ORANGE (N too low) |
| S5 | Settlement Time Arb | diff>1bps | 67 | 0% | 0.00 | -20% | RED |
| S6 | Mean Reversion | >1.5bps/16h | 144 | 49% | 1.44 | +217% | RED (directional bias) |
| S7 | New Listing Spike | >2bps | 2 | 100% | inf | +69% | YELLOW (N=2) |

**S6 Deep Dive:** Sharpe 1.53 appeared promising but all 144 trades were LONG (0 shorts). Positive funding = longs pay = strategy always goes long = directional bias. Feb -35% (bear), Mar +62% (bull). Not structural edge, just market direction.

**Key Insight:** Major coins rarely have extreme funding (BTC: 0 events >2bps in 6 months). Real edge lives on micro-cap coins with 15-200+ bps rates — exactly what our current engine already trades.

### 2026-04-20 — Trading Platform v1 + Funding Capture Live

**Funding Capture — первый полный автоматический цикл (20:00 UTC):**
- 6 позиций: entry за 5 сек → funding credited через WS → exit за 0.3 сек
- Hold time 2-3 секунды. 0 открытых позиций после. 0 ошибок.
- При $1k/10x: +$9.08 per settlement, ~$817/мес
- При $10k/max lev: +$190 per settlement, ~$17k/мес ($23k с referral)
- Breakeven: 11bps (7.7bps с referral)

**Exploit Research — 6 направлений проверено:**
- Basis Trade: RED (APR 0.6-2% в bearish)
- Token Unlocks: YELLOW (ARB -7...-15%, нужен платный API)
- Launchpool: YELLOW (нужен парсер анонсов)
- Liquidation Cascades: ORANGE (directional risk)
- Fee Rebate Mining: ORANGE ($10-50/day, отдельная MM система)
- Referral Rebate: GREEN (free -30% fees при scale up)

**Screener переведён на Bybit + dead coin filter (PLA/USDT bug fixed)**

### 2026-04-20 — Trading Platform v1: Engine + Redis + Telegram + Docker

**Trading Engine:**
- TradingEngine daemon — single-process asyncio, persistent event loop
- EventBus — typed async pub/sub (asyncio.Queue)
- BybitWebSocket — V5 public (tickers+funding) + private (auth+executions), auto-reconnect
- FundingCaptureStrategy — REST scan 573 пар → WS мониторинг hot ~35 → entry 10s before settlement
- ExecutionManager — market orders, funding entry/exit, TP/SL, rebalance
- PositionTracker, StateManager (SQLite), StrategyScheduler (4h/daily)
- Strategy config из `config/strategies.yml`

**Первый live тест (12:00 UTC settlement):**
- 13 монет scheduled, 9 позиций открыто реально
- Найдены баги: duplicate signals (WS ticks ~100ms), exit не отработал
- Оба бага пофикшены: dedup через `_traded_this_round` + execution lock
- PnL estimate (clean): +$0.037 (+0.25% на $15, ~$3.35/мес)

**Platform Architecture (Redis pub/sub):**
- RedisBus — async pub/sub wrapper, 5 каналов (signals, commands, notifications, events)
- Signal schemas — Pydantic models (ScreenerSignal, FundingSignal, TradeEvent, EngineCommand)
- Paper Trading Service — standalone daemon, subscribes to Redis signals
- Screener decoupled — публикует в Redis, PaperTrader как fallback
- Telegram Bot — bidirectional: /status, /positions, /start, /stop, /paper, /help + Redis forwarding
- Engine → Redis: commands subscription, trade events publish, status to KV
- Unified Docker Compose — 6 сервисов: redis, screener×2, paper-trading, engine, telegram-bot
- Bybit API: тестовый трейд SUPER (open 612ms, close 203ms, round-trip 815ms)
- Архитектура расширяема: Strategy ABC + EventBus позволяют добавлять стратегии без изменения ядра

### 2026-04-19 — Итоги сессии: от research к production

**Что сделано за сессию:**

1. **Paper trading инфраструктура** — SQLite DB, авто-трекинг TP/SL, CLI stats
2. **Big Move Detector** — standalone убыточен, совмещение с Miro избыточно
3. **Level Quality** — 22 фичи, WR +7.4pp, лучшая ML группа (21% importance)
4. **Swing/Hour/CIP** — marginal improvements, overfitting при комбинации
5. **Honest Walk-Forward ML** — 51 символ, 24 мес, 7 OOS windows: WR 31%, +1.8% annual
6. **Vision OOS** — 200 trades: score>=8 = 78% WR (balanced), ~55% real (Bayes)
7. **ML + Vision combined** — 407 OOS trades: score>=8 = **WR 58%, PF 3.46, ~7.3% annual**
8. **Volume Ranking L/S** — подтверждено Sharpe 1.62, +14.2% annual, market-neutral
9. **Funding Scalp** — average мёртв, HF capture >10bps = +$160/мес на $1k
10. **Multi-strategy architecture** — Strategy ABC + PortfolioManager, scalable для N стратегий
11. **Auto-launch** — Windows Task Scheduler: screener 4h, VR daily, paper check hourly, Telegram report daily
12. **Первый paper trade** — TCT/USDT SHORT, SL hit -4% (без Vision filter)

**Подтверждённый portfolio:**

| Стратегия | Annual | WR/Sharpe | Status |
|---|---|---|---|
| Miro + ML + Vision>=8 | +7.3% | WR 58%, PF 3.46 | Paper trading running |
| Volume Ranking L/S | +14.2% | Sharpe 1.62 | Paper trading running |
| Funding capture >10bps | +192% (tiny) | WR 56% | Waiting futures account |

**Опровергнуто:** Big Move standalone, funding scalp (average), coin pre-selection,
CV estimates (overfitted 4x), all simple TA strategies.

### 2026-04-19 — ML + Vision Combined Pipeline: BEST RESULT

Walk-forward ML (51 символ, 24 мес) → Vision scoring (407 OOS trades, ~$4 API cost).

| Pipeline | Trades | WR | PF | Exp/trade |
|---|---|---|---|---|
| ML only (thr>=0.50) | 407 | 30.7% | 1.09 | +0.13% |
| ML + Vision>=7 | 375 | 32.5% | 1.20 | +0.28% |
| **ML + Vision>=8** | **83** | **57.8%** | **3.46** | **+2.47%** |

Score distribution: score 3-4 = 0% WR (15t), score 7 = 25% WR (292t, = baseline),
**score 8 = 58% WR (83t)**. Vision score 8 = единственный значимый фильтр.

Pipeline: ML thr>=0.50 → Vision → trade only score 8 = ~6 trades/mo, cost $0.60/mo.

**Corrected annual estimate:** 83 trades / 14 мес = 6 t/mo, exp +2.47%, 4% risk.
Monthly +0.59%, **annual +7.3%** (не 157% — баг в period_months скрипта).
ML-only corrected: 29 t/mo, +0.13% exp → annual +1.8%.
Vision>=8 = x4 improvement over ML-only (7.3% vs 1.8%).

### 2026-04-19 — HF Funding Capture: работает при extreme filter

Симуляция стратегии Васи: leverage 10x, 5s hold, funding capture.
Price impact 5s = 6.4 bps (1m candle estimate).

| Filter | Trades/day | WR | Net/trade | Monthly |
|---|---|---|---|---|
| >2bps, taker | 5.6 | 5% | -$8.69 | -$1,470 |
| **>10bps, taker** | **0.7** | **56%** | **+$7.91** | **+$160** |

Работает ТОЛЬКО при extreme funding (>10bps). Вася оптимизирует: точный вход (2-3 сек),
4h funding cycle coins, жёсткий фильтр. Нужен futures account + WebSocket бот.

### 2026-04-19 — Vision OOS Test: 200 trades, score>=8 = 78% WR

Тест Claude Vision (Sonnet 4 через OpenRouter) на 200 OOS трейдах (100W+100L balanced,
20 символов, last 4 months). Cost: ~$2.

| Score | N | WR (balanced) | PF |
|---|---|---|---|
| >= 5 | 173 | 53% | 2.52 |
| >= 7 | 170 | 52% | 2.52 |
| **>= 8** | **51** | **78%** | **8.56** |
| 3-4 | 27 | 33% | <1 |

Correlation +0.209 — воспроизводит prior test (+0.208).

**Base rate correction** (balanced sample → real 25% WR):
Score>=8 sensitivity=40% (40/100 wins), specificity=89% (89/100 losses).
Real estimated WR at score>=8: **~55%** (Bayes adjusted). Всё ещё значительно выше ML (31%).

Vision = самый мощный фильтр. Оптимально: ML first (отсеять мусор) → Vision score>=8 → enter.

### 2026-04-19 — Volume Ranking Long/Short: подтверждено Sharpe 1.6

Стратегия из подкаста Scott Phillips (Hyper Trend fund, $20M AUM).
Правило: rank монеты по volume_7d/volume_30d, long top 50%, short bottom 50%, daily rebalance.

| Метрика | Его claims | Наш тест (net) |
|---|---|---|
| Return (11 мес) | +49.4% | +13.1% (без leverage) |
| Sharpe | 2.61 | 1.62 net / 1.95 gross |
| Max DD | 9.4% | 6.4% |

Разница: он вероятно использует 2-3x leverage + maker fees + 80 символов.
Short side доминирует (+0.22%/day vs -0.13%/day long).
8/12 месяцев прибыльные. Fee-sensitive: maker fees критичны.

Sensitivity: 14d/60d window лучше (Sharpe 1.74, turnover 5.5%).

### 2026-04-19 — Funding Scalp: DEAD

Avg funding rate 0.018% < fees 0.08%. 0% прибыльных трейдов.
Даже с colocation/maker rebates net отрицательный.
Price behavior вокруг funding: favorable direction только 35-45%.

### 2026-04-19 — Honest Walk-Forward ML: 51 символ, 24 месяца, 7 OOS windows

Предыдущий результат 14.3% annual был overfitted через 5-fold CV look-ahead.
Proper walk-forward (train 8mo, test 2mo, roll) на расширенном датасете:

**Данные:** 51 символ, 24 мес, 9212 трейдов total, 5890 в OOS.

| Config | Trades OOS | T/mo | WR | PF | Exp | Annual |
|---|---|---|---|---|---|---|
| **Top 10 features, strong reg** | **366** | **26** | **30.9%** | **1.17** | **+0.25%** | **+3.1%** |
| Top 15 features | 240 | 17 | 31.7% | 1.12 | +0.18% | +1.5% |
| All 37 features | 252 | 18 | 27.8% | 1.01 | +0.02% | +0.1% |
| No ML baseline | 9212 | 416 | 25.2% | — | -0.12% | -21.1% |

**Per-window consistency:** 4/5 windows прибыльные, 1 убыточный.

**Выводы:**
- Edge подтверждён: ML поднимает WR с 25% до 31% (breakeven 25% при RR 1:3)
- Feature selection критична: 37 фичей = breakeven, 10 фичей = +3.1%
- Honest annual ~3% — в 4-5x скромнее чем CV estimate (14.3%)
- Это типичная картина: research overfits, OOS скромнее, но edge > 0

### 2026-04-19 — Swing Structure + Hourly Bias + Coin-in-Play

Протестированы оставшиеся пункты Этапа 3, все поверх LQ baseline (14.3%):

| Config | T/mo | WR | PF | Annual |
|---|---|---|---|---|
| **LQ + Hourly Bias** | 18 | 39% | 2.45 | **+15.4%** |
| LQ baseline | 17 | 42% | 2.40 | +14.3% |
| LQ + Coin-in-Play | 13 | 44% | 3.00 | +14.2% |
| LQ + Swing | 14 | 43% | 2.67 | +13.4% |
| LQ + All combined | 14 | 40% | 2.44 | +12.7% |

**Swing structure:** 5.7% importance, sw_ll_count и sw_position_in_range полезны, но annual не растёт.
**Coin-in-Play:** cip_range_expansion = 3.7% importance (топ!), WR +3pp, но трейдов меньше.
**Hourly bias:** marginal +1.1pp annual, European session slightly better.
**All combined:** хуже baseline — overfitting от 19 новых фичей.

**Вывод:** Этап 3 завершён. LQ + hourly bias = 15.4% annual (новый best, marginal).
Swing и CIP фичи полезны индивидуально, но суммарно добавляют шум.

### 2026-04-19 — Level Quality + Position Sizing: новый лучший результат 14.3%

**Per-touch level quality (22 новых фичи):**
Для каждого касания уровня: volume (ratio, trend, max), wick rejection,
bounce speed, touch spacing, zone tightness. На 4H и D1.

| Метрика | Baseline | + LevelQuality |
|---|---|---|
| WR (best thr) | 38.8% | **46.2%** (+7.4pp) |
| PF | 2.12 | **3.91** (+85%) |
| Exp/trade | +1.42% | **+2.92%** (2x) |
| Trades/mo | 17 | 10 |
| Annual (3%) | +9.1% | +10.5% |

Top quality фичи (21% total importance):
- `d1_lq_touch_vol_trend` (3.6%) — объём растёт к последним касаниям
- `lq_max_touch_vol` (2.9%) — макс объём при касании
- `lq_max_wick_rejection` (2.0%) — длинные фитили = сильный уровень

**Position sizing:** adaptive по P(big_move) не лучше flat.
Простое увеличение risk 3% -> 4% даёт +12.3% annual (Sharpe тот же).

**Комбо (новый best): LevelQuality ML + Flat 4% risk = +14.3% annual, Sharpe 4.34.**

| Config | Annual | Sharpe |
|---|---|---|
| Baseline 3% | +9.1% | 3.97 |
| Baseline 4% | +12.3% | 3.97 |
| **LQ ML + 4% risk** | **+14.3%** | **4.34** |

### 2026-04-19 — Big Move Detector: standalone бэктест + совмещение с Miro

**Standalone (walk-forward, 16 символов, 1H, 7 конфигураций):**
Направление по RSI (>55 long, <45 short). Все конфиги убыточны.

| Config | Trades | WR | PF | Exp/trade |
|---|---|---|---|---|
| thr=0.5 RR2 | 595 | 39.7% | 0.88 | -0.16% |
| thr=0.7 RR2 | 396 | 36.1% | 0.79 | -0.29% |
| thr=0.7 RR3 | 407 | 32.9% | 0.81 | -0.25% |

**Причина:** модель предсказывает ЧТО будет движение (80% precision), но RSI не даёт direction.

**Совмещение с Miro d1_only (12 символов, 4H):**

| Config | T/mo | WR | PF | Exp | Annual |
|---|---|---|---|---|---|
| **Miro baseline** | **19** | **44%** | **2.61** | **+1.81%** | **+13.3%** |
| Miro + BM features в ML | 16 | 46% | 2.76 | +1.88% | +11.7% |
| Miro + BM>=0.7 filter | 4 | 48% | 4.48 | +3.45% | +4.7% |

**Вывод:** Big Move Detector не улучшает Miro стратегию. BM фильтр повышает WR (44→48%) и PF (2.6→4.5), но режет количество сделок (19→4/мес), annual падает. BM фичи в ML нейтральны (~4% importance). Miro уже ловит движения через level breakout/retest — двойной фильтр избыточен.

**Решение:** Big Move Detector → backlog. Miro d1_only + ML = 13.3% остаётся best.

### 2026-04-19 — Paper Trading инфраструктура

Реализован paper trading tracker:
- SQLite БД (`data/paper_trades.db`) — автозапись каждого алерта
- Авто-проверка TP/SL при каждом скане скринера
- CLI: `python scripts/paper_trading.py status|check|stats|history`
- Первый paper trade: TCT/USDT SHORT ZAKOL @ 0.00312

### 2026-04-19 — Reverse Pattern Discovery: предикторы сильных движений

**Подход от обратного:** вместо "вот паттерн, работает ли?" — "вот движение >5%, что было до него?"

16 монет, 1H, 6 мес. Найдено 951 strong move (>5% за 6 часов).

**ML precision (предсказание "скоро будет big move"):**

| Threshold | Predicted | Precision | Recall |
|---|---|---|---|
| 0.5 | 473 | 67.9% | 43.7% |
| 0.7 | 211 | **80.1%** | 23.0% |
| 0.8 | 134 | **83.6%** | 15.3% |

**Top предикторы:**
1. `hour_utc` — 12-13 UTC самый активный
2. `atr_pct` — волатильность повышена (+30%) перед movement
3. `avg_lower_wick_pct` — длинные тени = борьба
4. `volume_spike` — объём растёт ДО движения
5. `volatility_contraction` — squeeze → explosion
6. `rsi` — для crash >12%: RSI ~40, ниже SMA

**Найденные паттерны:**
- Squeeze → Explosion (сжатие волатильности → резкое движение)
- Volume precedes price (кто-то набирает позицию)
- Wicks = борьба (когда заканчивается → direction)
- Crash pattern: RSI 40 + ниже SMA + ещё падает

**Самые предсказуемые:** INJ, SUI (AUC 0.66), LINK, SOL, NEAR (0.70+)

Скрипт: `scripts/research/reverse_pattern_discovery.py`

### 2026-04-19 — Multi-TF D1 levels: 4H/d1_only + ML = 13.3% годовых

**Главный прорыв: D1 уровни кардинально улучшают качество сигналов.**

Бэктест Multi-TF (D1 levels + 4H entries + ML filter):

| Config | Trades/мес | WR | PF | Annual |
|---|---|---|---|---|
| 4H/base (no D1, no ML) | 164 | 24.2% | 0.85 | -11.3% |
| 4H/base + ML | 38 | 31.5% | 1.26 | +4.1% |
| **4H/d1_only + ML 0.25** | **19** | **44.3%** | **2.61** | **+13.3%** |
| 4H/d1_only + ML 0.40 | 13 | 50.0% | 3.32 | +11.2% |
| 4H/d1_only + ML 0.55 | 7 | 58.7% | 5.22 | +9.2% |

Вывод: уровни с D1 объективно сильнее. При R:R 1:3 и 44% WR — edge реальный.

Также проведено:
- Бэктест 1H (standard/tight/scalp) — 1H без ML убыточен, с ML +3-6%
- Vision model comparison (sonnet-4.6, haiku-4.5, gpt-4o × 3 промпта) — sonnet-4.6/binary лучший (corr +0.208), но сигнал слабый vs ML
- D1 alignment analysis: trades near D1 levels = 26% WR vs 23% без D1

Обновлён скринер:
- `d1_mode: d1_only` — использует D1 уровни для детекции сигналов
- Новая ML модель `miro_gb_d1.joblib` обучена на D1 данных
- Задеплоен на сервер: `miro-screener-4h-d1` + `miro-screener-1h`
- Telegram алерты подключены

### 2026-04-18 — Screener v1: production-ready signal scanner

**Этап 2 Miro Strategy — screener реализован.**

Архитектура:
- `src/strategy/` — level detection, signals, features (extracted from research scripts)
- `src/screener/` — async scanner, coins-in-play detector, state persistence
- `src/ai/` — ML scorer (joblib), Vision scorer (OpenRouter), chart generator
- `src/api/telegram/` — alert bot with chart attachments

Возможности:
- Скан 50+ монет каждые 4h (aligned to Binance candle close)
- Rolling level detection + breakout/retest/закол
- ML score (GradientBoosting P(win)) с threshold фильтрацией
- Claude Vision score (optional, ~$0.004/запрос)
- Telegram alerts с графиком
- Coins-in-play: автоматическое добавление high-volume/big-mover монет
- State persistence (JSON) для breakout tracking между runs

Запуск:
- `python scripts/train_model.py` — обучение ML модели
- `python scripts/run_screener.py --once` — single scan
- `python scripts/run_screener.py` — continuous mode

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
