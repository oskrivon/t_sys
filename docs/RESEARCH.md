# Исследования и решения

## Архив

- [Weekend Threshold Regression + 2 dead-end фикса](archive/WEEKEND_THRESHOLD_RESEARCH.md) — **FIX + 2 DEAD ENDS**. Live 1/7 (луз-стрик 6) — причина не в сигнале: прод дрейфанул на распущенный порог **3-of-5** (count), валидация требовала **4-of-5** (net). Ре-валидация 6/6: 4of5 Sharpe 2.10 [CI 1.46-2.79] vs 3of5 0.79 [CI −0.04-1.54]; 150 маржинальных 3of5-сделок дают total −22.6%. Порог исправлен 3→4. Отклонены: расширение окна на дни (с SL строго хуже) и гейт corr>0.55 (эффект перевёрнут — эдж сильнее при НИЗКОЙ корреляции).
- [News Event Trading: Strategy/Saylor BTC Sale](archive/NEWS_EVENT_TRADING_RESEARCH.md) — **NOT ACTIONABLE**. Strategy продал 32 BTC (1 июня), BTC -6% за 4 дня. Наш weekend шорт поймал хвост (entry $61.8k, но reversal в вс → -1.62%). Систематизировать нельзя: N=2 за всю историю, latency vs algo-фонды, on-chain не видно (OTC/малый объём).
- [Spread Trading: Calendar, Basis, Cross-Asset](archive/SPREAD_TRADING_RESEARCH.md) — **DEAD END (все 4 типа)**. Calendar: funding=basis, ликвидность quarterly 0.1% от perps. Dynamic basis: MM держат <1 bps. Cross-asset: ни одна пара не коинтегрирована стабильно (BTC/ETH 33% окон). De Prado spread approach не применим к крипте из-за perpetual funding mechanism.
- [Weekend Exit Timing](archive/WEEKEND_EXIT_TIMING_RESEARCH.md) — **DEAD END**. Hourly profile, conditional exit (Sat DOWN), TP, trailing stops, L/S split exit. Лучший кандидат L=Mon04/S=Sun23 (Sharpe +0.25) не прошёл robustness: paired t-test p=0.13, bootstrap 95% CI содержит 0, underpowered (71/234 trades). Keep Sun 23:00.
- [Signal Processing Denoising: Wavelet + Kalman](archive/SIGNAL_DENOISING_RESEARCH.md) — **DEAD END (strategy), USEFUL (Kalman velocity as tool)**. Wavelet denoising вреден (non-causal look-ahead), Kalman velocity — единственный полезный компонент: direction accuracy 54% vs RSI 47%, PF 1.25 vs 0.73. Но standalone стратегия 0/8 validation.
- [Astrology / Zodiac / Mercury Retrograde vs BTC](archive/ASTROLOGY_BTC_RESEARCH.md) — **DEAD END**. Mercury Retrograde, лунные фазы, зодиакальные транзиты — ноль edge по всем 12 знакам. Google Trends "horoscope" коррелирует с волатильностью (r=0.28, p<0.001), но это прокси retail attention, не астрология.
- [Weekend SL Recovery](archive/WEEKEND_SL_RECOVERY_RESEARCH.md) — **DEAD END**. После SL hit при сильном консенсусе BTC не восстанавливается (WR 40%, avg -0.20%). Re-entry ухудшает Sharpe, flip = noise. Принять SL loss — оптимально.
- [Equity Market / LLM Earnings / Micro-Cap](EQUITY_MARKET_RESEARCH.md) — **DEAD END**. PEAD edge ~0.1%/trade (negligible). MOEX micro-cap нет шорта. US 8-K dilution: costs=edge. Фондовый рынок не подходит для нашего масштаба.
- [Cascade Tick Bars](archive/CASCADE_TICK_BARS_RESEARCH.md) — **DEAD END**. Tick bars (500) и imbalance bars для cascade trigger detection. На 3 днях WR=100%, Sharpe=1.27 — ложный сигнал. На 90 днях (144 trade, 5 монет) WR=47%, net=-0.10%/trade, Sharpe=-3.4. MFE≈MAE → направление после триггера случайное. Инфра полезна: стриминговый парсер Binance Vision aggTrades.
- [Tick Bars + Big Move Direction](archive/CASCADE_TICK_BARS_RESEARCH.md) — **NO IMPROVEMENT**. Tick sell% не улучшает direction prediction для big moves. RSI остаётся лучшим direction picker (относительное сравнение, абсолютные цифры с look-ahead bias).
- [Informed Flow Detection](archive/CASCADE_TICK_BARS_RESEARCH.md) — **DEAD END**. Absorption (vol>2x, low price impact), stealth buy/sell (sell% divergence), breakout after quiet. 2090 signals / 90 дней / 5 монет. MFE/MAE=0.99 по всем типам — чистый random. Крипто фьючерсы не имеют "informed flow" как акции.
- [TradFi Tick Shadow](archive/CASCADE_TICK_BARS_RESEARCH.md) — **DEAD END**. NQ big day (|ret|>1%) → BTC catch-up post-close. 44 events / 180 дней. Follow rate 48% (random). Desync filter WR=40% (хуже random). Tick sell% post-close = 47-53% на всех событиях — нет directional flow. Крипто не "тень" equity на тиковом уровне.
- [CVD (Cumulative Volume Delta) as Feature](archive/CVD_FEATURE_RESEARCH.md) — **DEAD END**. 11 CVD-фичей (slope, price divergence, buy ratio, vol delta z-score) на 4H ТФ, 8 монет, 461 сигнал. Все |corr| < 0.05. ML accuracy 47.5% (хуже random 50.1%). Фильтр работает в обратную сторону. **Закрывает весь блок buy/sell classification в крипте: TIB, VIB, DIB, CVD — всё мертво.** Причина: wash trading + delta-neutral MM + нет informed flow.

## Активные исследования

- **V-Bottom Dip Buying** — **PAPER TRADING**. BTC drop 3%/24h + NATR high + NQ T-1 not down → buy, hold 24h. Honest CAGR +25%/yr, Sharpe 1.05, MaxDD -24%. Smart beta (BTC long timing), not alpha. Look-ahead bias found in original NQ filter (+timestamp shift test added to scorecard). [Детали](V_BOTTOM_RESEARCH.md)
- **Weekend SL + V-Bottom Re-Entry** — **SUPERSEDED**. Was: SL 0.75% + bounce re-entry, Sharpe 2.09. Now: no SL (catastrophe 5% only), compound CAGR +118% vs +73% (SL 2.5%), p=0.006. Re-entry disabled. [Детали](WEEKEND_SL_REENTRY_RESEARCH.md)
- **Weekend Effect: Cross-Asset -> BTC** — **LIVE**. 5-predictor ensemble (KWEB, EWJ, XLK, XLE, USDJPY), **majority 4/5** (исправлено с 3/5 2026-06-14 — порог дрейфанул от валидации, см. [Threshold Research](archive/WEEKEND_THRESHOLD_RESEARCH.md)). Exit Sun 12:00 UTC, catastrophe SL 5%. На свежем периоде (2021-2026) 4of5 Sharpe ~2.1; на 8.6 годах хедлайн 2.82 (эдж сжался). MFE analysis proves any trailing/BE hurts. [Детали](WEEKEND_EFFECT_RESEARCH.md)
- **Calendar Events: FOMC + Q-Expiry** — **CONFIRMED OOS**. Pre-FOMC LONG (WR 70%, 8/yr) + Post-Q-expiry SHORT (WR 58%, 4/yr). Combined Sharpe_net 1.06, ~14% annual. Zero overlap с weekend signal.

- **Icebreaker (Wall-Eating)** — **DEAD (эдж субкомиссионный)**. Их +10.75% не воспроизводится. Faithful-тест март-2026 на free Bybit (абсорбция через трейды). Config sweep по обоим листам Excel: на их грид-оптимуме (TP1.2/SL0.2) у TAO **реальный gross +2.85%**, но эдж 0.049%/сделку < комиссии (maker 0.06%/taker 0.11%) → нетто-минус (портфель TAO+ZEC+SUI: gross +3.60% / net −4.54% taker). Эдж есть, но тоньше издержек (квант-тезис: breakeven 0.041%/сделку). Time-exit отрицателен (mean-reversion). Актив: 8.8GB единого parquet-store (Bybit L2+trades март-2026) + инфра (ветка `icebreaker-collector`, 99 тестов). [Детали](ICEBREAKER_RESEARCH.md)
- **Funding Capture HF** — **ACTIVE**. Live trading engine на Bybit. [Детали](FUNDING_CAPTURE_RESEARCH.md)
- **Miro Strategy + Claude Vision** — **MAIN TRACK**. Screener + Claude Vision filter. Score>=7 → 75% WR на 36 trades (100 sample)
- [Арбитраж — финальный отчёт](../data/reports/arbitrage_final_report.html) — DEAD: 6 треков проверено, все мертвы
- [Funding rate arbitrage](FUNDING_ARB_RESEARCH.md) — passive yield 3.5-5% APR. Backlog.
- [Cross-exchange арб](ARBITRAGE_RESEARCH.md) — Этапы 1+2 завершены, дальнейшая работа отложена в tail
- [Стек и латентность](STACK_RESEARCH.md) — выбор языка для trading infrastructure
- [Настройка бирж](EXCHANGES_SETUP.md) — API ключи Binance/Bybit/OKX

## Решения

### [Flip-стратегии: систематический тест 5 подходов] — 2026-05-19

**Концепция:** вход по direction signal, confirm window, если wrong — flip position.
Протестировано 5 вариантов, 3 рабочих:

| Стратегия | Flip effect | OOS Sharpe | Trades/yr | Status |
|---|---|---|---|---|
| **Weekend reversal** | **+32% Sharpe** | **4.41** | ~20 | **Live (deployed)** |
| Squeeze + momentum | Reduces loss | -0.06 | ~12 | Dead end — direction=random |
| **Squeeze + funding** | Marginal | **1.11** | ~8 | **Backlog** |
| **TradFi NQ lag** | **Hurts** (-15%) | **0.74** | ~23 | **Backlog** (no flip) |
| Opening Range | Marginal (+0.04) | 0.14 | ~41 | Dead end — 4h too coarse |

**Ключевые выводы:**
1. Flip работает когда confirm window достаточен для determination (weekend 24h = ok)
2. Flip вредит когда edge = delayed catch-up (TradFi lag: BTC ещё не догнал NQ, flip прерывает)
3. Flip бесполезен если direction signal = noise (squeeze + momentum WR 37%)
4. Quality direction signal (WR >= 55%) — необходимое условие для любой flip стратегии

**Скрипты:** `scripts/tmp/weekend_reversal_wf.py`, `scripts/tmp/squeeze_funding_multi.py`,
`scripts/tmp/tradfi_lag_flip.py`, `scripts/tmp/opening_range_flip.py`,
`scripts/tmp/squeeze_flip_backtest.py`

---

### [Weekend Ensemble: 8.6 лет, Sharpe 2.82, каждый год в плюсе] — 2026-05-14

**Контекст:** Расширен BTC 4h датасет до 2017-08 (8.7 лет, 19127 свечей). Полный re-run
validation pipeline + deep dive в характеристики wins vs losses + divergence analysis.

**Стратегия:** VOTE(BABA_fri + NQ_fri + Tech_week) → BTC weekend (Fri 21:00 → Sun 23:00 UTC).
Majority vote 3/3, SL 2%.

**Результаты на 8.6 годах (162 trades):**

| Метрика | Значение |
|---------|----------|
| Annual (full capital) | **21.4%** |
| Annual ($1k/$10k = 10% alloc) | 2.0% |
| Win rate | 61% |
| Avg return | +1.13% |
| Max drawdown | -9.1% |
| Sharpe | 2.82 |
| Calmar | 2.35 |
| Profit factor | 4.14 |
| Profitable years | **9/9 (every year)** |
| Trades/year | ~19 |

**Validation scorecard: 6/7 PASS**
- CPCV median Sharpe 1.86, P(>0)=100% — PASS
- DSR p=0.017 — PASS
- MinBTL — FAIL (need 8y for 202 trials, have 4.8y BTC; при n_trials=1 PASS)
- PBO 0.35 — PASS
- Factor alpha t=3.68, p<0.001 — PASS (не объясняется BTC/momentum/size)
- Multi-regime: PASS (low-vol Sharpe 1.49, high-vol 2.64)

**Walk-forward H1→H2: 7/7 PASS**
- H1 best → H2 OOS: Sharpe 3.63 → 2.19, WR 60%, alpha t=2.24

**Deep dive — что разделяет winners и losers:**

1. **BTC week return = главный модулятор:**
   - Flat week (|ret|<3%): WR **75%**, avg +1.38%, Sharpe **3.72** (N=61)
   - Up week (>+3%): WR **50%**, avg +0.86%, Sharpe 2.09 (N=62)
   - Причина: после сильного роста weekend скорее корректируется

2. **Direction:** LONG (Sharpe 3.16) > SHORT (2.46) на длинной дистанции. BTC uptrend.

3. **Convergence trade подтверждён (N=32):**
   Equity bullish + BTC down week → long BTC weekend = WR 72%, avg +1.47%.
   BTC подтягивается к тому, куда ушла фонда.

4. **Сезонность:** Q1 (Jan-Apr) WR ~83%, Q3 (Jul-Oct) WR ~42%. Summer doldrums.

5. **Divergence magnitude:** small divergence лучше (WR 73%), extreme хуже (WR 49%).
   Но на большой выборке корреляция div↔return = 0.000. Не самостоятельный предиктор.

**Trailing stop — не работает:**
Любой trailing (1-2.5%) убивает результат. Лучший conditional (trail 1% after +2%) даёт
Sharpe 2.11 vs baseline 2.61. Причина: BTC дёргается внутри weekend, trailing ловит шум.
Стратегия зарабатывает на терпении (hold до Sunday close).

**Saturday dip recovery — миф:**
Trades которые дипнули >1% в Saturday AM: WR 50%, avg +0.13% — coinflip.
Trades без dip'а: WR 71%. "Зайти после dip'а" = усреднение в убыточную позицию.

**Leverage analysis:**

| Allocation | Annual | Max DD | Worst trade |
|------------|--------|--------|-------------|
| 100% capital (1x) | 21.4% | -9.1% | -2.0% |
| 3x leverage | 64% | -27% | -6.0% |
| 5x leverage | 107% | -45% | -10.0% |
| 10x leverage | 214% | -91% | -20.0% |

Рекомендация: 3-5x с conditional SL ордером на бирже (не cron каждые 4ч).

**Решение:** стратегия подтверждена на 8.6 годах. Готова к live. Не тюнить, зафиксировать
ensemble. Фильтр |BTC week|<3% записан как гипотеза для paper tracking, не хардкод.
Капитал в будние дни свободен для funding capture.

**Скрипты:** `scripts/research/validate_weekend_macro.py`,
`scripts/tmp/weekend_deep_analysis.py`, `scripts/tmp/weekend_divergence_full.py`

---

### [7 Funding Exploits — все RED кроме текущего capture] — 2026-04-22

**Контекст:** систематическая проверка 7 exploit-идей на уровне механик бирж. Все протестированы на 10 символах, 6 мес, единый cost model (Bybit futures ~16 bps/trade). Использован новый `src/backtest/` модуль.

**Результаты:** 0 из 7 показали надёжный structural edge на major монетах. S6 (mean reversion, Sharpe 1.53) оказался directional long bias — все 144 трейда LONG, Feb -35%. S5 (settlement arb) WR=0%. Остальные — отрицательная или нулевая expectancy.

**Root cause:** BTC имел 0 событий >2bps за 6 мес. Major coins = efficient market для funding. Edge живёт на micro-cap (15-200+ bps rates) = то что engine уже делает.

**Решение:** funding capture на мелких монетах остаётся единственным working structural exploit. Следующий шаг — scale up капитала и добавление Binance.

**Скрипт:** `scripts/research/exploit_7_strategies.py`, `scripts/tmp/run_7_exploits.py`

---

### [Structural Exploits — 6 направлений проверено] — 2026-04-20

**Контекст:** систематический поиск structural edges помимо funding capture. Фреймворк: "где деньги перемещаются предсказуемо?" Проверено 6 направлений за 1 сессию.

**Результаты:**
- Basis Trade: **RED** — APR 0.6-2% в bearish рынке (нет premium)
- Token Unlocks: **YELLOW** — ARB -7...-15% после каждого unlock. Стабильно 3/3 месяцев. Нужен платный API для дат.
- Launchpool: **YELLOW** — edge вероятен, data inconclusive. Нужен парсер анонсов.
- Liquidation Cascades: **ORANGE** — directional strategy, не exploit. OI данные есть.
- Fee Rebate Mining: **ORANGE** — $10-50/day но отдельная MM система.
- Referral Rebate: **GREEN** — бесплатные -30% fees при scale up.

**Решение:** funding capture остаётся единственным working structural exploit. Token Unlocks и Launchpool в backlog как высокоприоритетные — token unlock -7% с высокой уверенностью = реальные деньги при scale.

**Детали:** `docs/EXPLOITS_BRAINSTORM.md`

---

### [Level Quality + Position Sizing — best result 14.3%] — 2026-04-19

**Контекст:** поиск способов улучшить Miro d1_only + ML (13.3% baseline).

**Per-touch level quality (22 фичи):** для каждого касания уровня считаем volume, wick rejection, bounce speed, touch spacing. WR +7.4pp, PF 2.12->3.91, exp x2. Trades/mo падают (17->10), но quality компенсирует.

**Position sizing:** adaptive по P(big_move) не лучше flat. Flat 4% > adaptive 2-5%.

**Big Move Detector:** standalone убыточен (предсказывает timing, не direction). Совмещение с Miro избыточно — режет трейды без пропорционального роста WR.

**Решение:** LevelQuality в ML + risk 4% = 14.3% annual (CV estimate).
**UPDATE:** Honest walk-forward OOS на 51 символе, 24 мес = **+3.1% annual.** CV overfitted ~4x.
Edge реален (WR 25%->31%), но скромный. Feature selection (10 фичей) критична.

---

### [Claude Vision OOS: 55% real WR, best filter] — 2026-04-19

**Контекст:** OOS тест на 200 balanced трейдах (20 символов, last 4 months).
Score>=8: 78% WR balanced, ~55% Bayes-adjusted (real base rate 25%).
Correlation +0.209 воспроизведён. PF 8.56 на score>=8.

**Решение:** Vision = финальный фильтр после ML. ML отсеивает мусор (25%→31% WR),
Vision оставляет только лучшие сетапы (31%→~55% WR). Cost $0.01/trade, ~$3/мес.

**DONE:** ML + Vision>=8 combined pipeline: 407 OOS trades, 51 символ, 24 мес.
WR 57.8%, PF 3.46, ~6 trades/mo. Score 3-4 = 0% WR (антисигнал). Score 7 = baseline (бесполезен).
**Только score 8 даёт edge.** Это финальный pipeline для Miro стратегии.

---

### [Claude Vision как "eye test" filter — validated] — 2026-04-18

**Контекст:** Автоматическая стратегия (breakout+retest) даёт 25% WR — убыточна.
Человек фильтрует сетапы "на глаз" и получает 35-40% WR — прибыльна.
Вопрос: может ли Claude Vision заменить человеческий eye test?

**Тест:** 100 trades (50 win + 50 loss), скриншоты графиков с уровнями,
Claude Sonnet 4.6 через OpenRouter оценивает сетап 1-10.

**Результат:**
- Avg score wins: 6.4, losses: 5.0, correlation +0.429
- Score >= 7: **36 trades, 75% WR** (p < 0.01)
- При R:R 1:3 и 75% WR: expectancy +2R/trade
- Latency 3-4 сек, cost $0.004/запрос

**Решение:** Claude Vision — primary filter в screener. Стратегия: screener находит
паттерны → Claude Vision оценивает → торгуем только score >= 7.

**Консервативная оценка:** 50-60% WR в live → 30-60% годовых при R:R 1:3.

---

### [Miro Strategy: автомат убыточен, нужен eye test] — 2026-04-18

**Контекст:** Формализация стратегии из Miro (breakout + retest горизонтальных уровней).
v1: 5/8 монет в плюсе (look-ahead bias). v2 out-of-sample: +2.4%.
v3 rolling walk-forward: -6.6%. ML filter: +6.6%.

**Проблема:** автоматический level detector генерит слишком много шума.
1,218 сигналов, 77% — losses. Человек бы взял 50-80 из них.

**Root cause:** 5 аспектов eye test не формализованы:
1. Качество касаний уровня (volume, wick, speed) — частично решено ML
2. Swing structure (HH/HL/LH/LL) — не реализовано
3. Multi-TF confirmation — не реализовано
4. "Монета в игре" (volume, social) — ML нашёл как top feature
5. Visual pattern as whole — **решено через Claude Vision**

**Решение:** пивот на screener + Claude Vision вместо полного автомата.

---

### [Pure arbitrage — solved problem, pivot to directional] — 2026-04-17

**Контекст:** После Gate onchain-1 проверили две дополнительные ниши:

**C1: Alt DEX pools (Arbitrum):**
- ARB/WETH 0.05% — арбитражится как WETH/USDC (gap 5s, боты активны)
- GMX/WETH, LINK/WETH 0.3% — нет ботов, но нет и ликвидности (232, 105 свопов/день), fee 30 bps

**C3: Small CEX (MEXC, Gate.io, Bitget) vs Binance:**
- 0 арб-окон на ETH/USDT и BTC/USDT (real-time, 2h sample)
- MMs арбитражят даже мелкие биржи на top-парах

**Решение:** Чистый арбитраж — solved problem на всех проверенных фронтах:
1. Cross-exchange CEX: MMs закрывают за мс (Gate 2)
2. Funding rate: fees > signal (Gate funding-1)
3. On-chain CEX-DEX: реалистично 2-23% APR с учётом slippage/competition
4. Alt DEX pools: либо тоже арбитражатся, либо нет ликвидности
5. Small CEX: тоже арбитражатся

**Следующий шаг:** пивот на directional strategies (momentum, patterns, event-driven).
Инфра (CCXT, backtester, TimescaleDB) пригодится.

**Отчёты:** `data/reports/alt_pools_arb_2026-03-01.md`, `data/reports/small_cex_arb_2026-04-17.md`

---

### [Gate onchain-1 — Arbitrum best track, EOA prototype next] — 2026-04-17

**Контекст:** Pool state analysis L1 + Arbitrum. Цена пула между свопами vs CEX mid.

**Ключевые findings:**
1. **Staleness bias** — без freshness filter APR завышен 5-10x (stale пул = мёртвые спреды 92-166 bps)
2. **L1 execution timing** — 87% свежих окон короче 1 блока (12s). Только 34/264 исполнимы
3. **Arbitrum competition** — 70.7% окон закрыто ботами (vs 49.4% L1). FIFO != нет конкуренции
4. **Arbitrum APR** — 78 fresh окон/день, все исполнимы (block 0.25s). ~241% APR при $10k, 20% capture

**Сравнение всех треков:**

| Трек | APR | Капитал | Конкуренция |
|---|---|---|---|
| **On-chain Arbitrum** | **120-360%** | $5-10k | Средняя |
| On-chain L1 | 60-180% | $10-25k | Высокая (MEV) |
| Cross-exchange CEX | ~20% | $10k + colocation | Очень высокая (MMs) |
| Funding rate | 3.5-5% | Any | Низкая |

**Решение:** YELLOW — переход к Этапу 2 (EOA prototype на Arbitrum). Нужен live тест capture rate и slippage.

**Детали:** `docs/ONCHAIN_ARB_RESEARCH.md` → "Gate onchain-1"

---

### [Funding rate — yield product, not trading alpha] — 2026-04-17
**Контекст:** Завершён Этап 1 funding research. 20 пар × 3 биржи × 12 месяцев.

**Ключевой результат:** все активные стратегии отрицательны после комиссий. Только buy-and-hold прибылен на 3.5-5% APR на select парах (BTC, ETH, LINK, SUI). Cross-exchange funding arb 0/114 прибыльных.

**Решение:** оставить как passive yield baseline для idle capital на биржах. Не main trading track. Пивот на on-chain CEX-DEX research.

**Детали:** `docs/FUNDING_ARB_RESEARCH.md` → "Результаты Этапа 1", отчёты `data/reports/funding_analysis.md` и `data/reports/funding_backtest_v2.md`

---

### [Gate 2 — Cross-exchange в tail, пивот на funding + on-chain] — 2026-04-17
**Контекст:** Завершён Этап 2 (Tardis L1 валидация free sample на 2026-03-01). Получены честные L1-цифры для сравнения с trades-proxy.

**Цифры:**
- PROXY vs L1 OPT: precision 85-93%, recall 73-93% — proxy валиден
- L1 OPT: BTC 15 окон, ETH 47 окон — **100% окон ≤1 секунды**
- **L1 SNAP = 0 окон** — при опросе раз в секунду не увидеть НИЧЕГО

**Ключевое открытие:** реальная длительность cross-exchange spot арб-окон — **миллисекунды**, не секунды. HFT/MMs закрывают спреды внутри секунды.

**Анализ требуемой инфраструктуры:**
- Rust vs Python: экономит 1-5 ms на CPU, но сеть (50-300 ms) — это 95% latency budget. Rust не решает проблему.
- Прямой кабель Москва→биржа: physically limited скоростью света (~150ms до Токио). Не решение.
- **Colocation в регионе биржи (AWS Tokyo/Singapore/HK)** — правильный подход. VPS $20-200/мес, latency 20-50ms в регионе.
- Market makers (Jump, Wintermute) уже colocated везде с FPGA/C++ — наш реалистичный fill rate 20-40%, не 80%.

**Пересмотренная экономика cross-exchange:**
- 63 окна/сутки × 30% fill × 4 bps × $10k = ~$2k/мес на $10k капитала (~20% APR)
- Приемлемо, но не бест-в-классе

**Решение:** **Cross-exchange WS переносится в tail roadmap-а**. Строить когда готова остальная инфраструктура. Приоритет переключается на:

1. **Funding rate arbitrage** (main) — APR 8-30% без плеча, Python-friendly, без HFT-конкуренции. Новый research-док: `docs/FUNDING_ARB_RESEARCH.md`
2. **On-chain CEX-DEX research** (parallel, $0) — через The Graph + EOA swaps, без смарт-контрактов. Новый: `docs/ONCHAIN_ARB_RESEARCH.md`
3. **Triangular** (side quest) — mini-research на альткоинах, PLAN.md

**Почему:**
- Funding arb работает в горизонте часы-дни — Python на равных, нет гонки latency
- On-chain research полностью бесплатный, хорошо дополняет картину
- Cross-exchange не отменяется, просто не первым, и нужна колокация

**Что не делаем:**
- Не тратим $300 на Tardis paid — trades-proxy валиден
- Не переходим на Rust прежде времени — проблема в сети, не в CPU
- Не строим WS-collector для research — теперь мы знаем, нужен он для production-арба (когда дойдём до Phase 5+)

**Детали:** `docs/ARBITRAGE_RESEARCH.md` → "Результаты Этапа 2", отчёт `data/reports/gate2_proxy_vs_l1_2026-03-01.md`

---

### [Gate 1 — Arbitrage Этап 1 пройден] — 2026-04-16
**Контекст:** Завершён Этап 1 исследования (trades-based proxy) на BTC/USDT + ETH/USDT по Binance/Bybit/OKX spot за март 2026.

**Цифры:**
- BTC: 138 окон, 4.4/сутки, медиана net_bps=4.0, медиана длительности **1с** (max 2с)
- ETH: 563 окна, 18/сутки, медиана net_bps=4.8, медиана длительности **1с** (max 7с)
- 92-96% окон — ровно 1 секунда (на trades-proxy)

**Решение:** **Зелёный свет** на Этап 2 (Tardis валидация, бюджет до $50).

**Почему:**
- Окна существуют, прибыль p99 27-36 bps — окупает round-trip fees
- Trades-proxy — нижняя граница реальности; quoted-окна могут быть значительно длиннее
- Tardis sample за $10-50 разрешит ключевой вопрос "1с на proxy = 1с на L1 или 30с?"
- Стоимость ошибки (месяц WS-инфры впустую) >> $50

**Риски:**
- Медиана 1с — на грани возможного для Python+WS даже с геолокацией
- Net_bps медиана 4-5 — slippage и latency могут съесть всё

**Критерий Gate 2:** после Tardis sample — если L1 окна ≥10с медиана → строим WS под production. Если ≤2с — пивот на triangular/funding/CEX-DEX.

**Детали:** `docs/ARBITRAGE_RESEARCH.md` → раздел "Результаты Этапа 1", отчёт `data/reports/arb_trades_2026-03.md`

---

### [Методология арбитражного исследования] — 2026-04-16
**Контекст:** Нужно оценить прибыльность cross-exchange spot арба (BTC/ETH vs USDT на Binance/Bybit/OKX) и требования к инфраструктуре перед тем как вкладываться в Phase 2-4.

**Исходная гипотеза:** скачать бесплатные L1 bookTicker архивы за последний месяц, прогнать анализ за вечер.

**Probe показал:**
- `data.binance.vision/spot/` — bookTicker **вообще не выкладывают** (только klines/trades)
- `data.binance.vision/futures/um/bookTicker/` — есть **только узкое окно** (март 2024 и около), свежего нет
- Bybit `public.bybit.com` — только daily trades csv.gz (формат по имени файла)
- REST API с ключом — **не даёт** исторических bid/ask ни на одной бирже
- On-chain — CEX-сделки off-chain (БД), только capital flows; orderbook не восстановить

**Варианты рассмотренные:**
1. WS-collector своими силами — $0, но ждать 2-4 недели
2. Tardis.dev — ~$100-300/месяц за L1 по 3 биржам или ~$10-50 за sample
3. Trades-based proxy из bid/ask по `isBuyerMaker/side` — coarse но бесплатно
4. Гибрид: сначала coarse, потом Tardis, потом WS

**Решение:** Вариант 4 — последовательный pipeline с gates:
1. **Этап 1 ($0):** trades-based proxy, coarse ответ H1
2. **Gate 1:** сигнал положительный → Этап 2; отрицательный → пивот (triangular/funding/CEX-DEX)
3. **Этап 2 ($10-50):** Tardis sample, L1-валидация proxy
4. **Gate 2:** L1 подтверждает → Этап 3; нет → пересмотр методологии
5. **Этап 3:** WS-collector — уже **не ради research**, а под production (Phase 2)

**Почему:**
- Каждый шаг валидирует предыдущий, можем остановиться на любом
- Самое дорогое (WS-стек + OMS + risk) строим только после green gate
- Не тратим 3 недели на WS с риском «а арба нет» в результатах
- Tardis не заменяет WS — WS нужен под live-торговлю, research это не снимает

**Детали:** `docs/ARBITRAGE_RESEARCH.md`

### [Основной стек] — 2025-02-22
**Контекст:** Нужна гибкая торговая инфраструктура для CEX арбитража
**Варианты:**
1. Python (ccxt + asyncio) — быстрая разработка, 250-500μs латентность
2. Rust — 6-12μs, но долгая разработка
3. Go — 50-100μs, компромисс
4. Hybrid (Python + Rust core) — лучшее из обоих миров

**Решение:** Python для MVP, Rust для критичных путей если понадобится
**Почему:**
- Крипто-биржи сами имеют latency 50-100ms — наши 500μs не bottleneck
- Быстрая итерация важнее micro-optimization
- NautilusTrader показывает что hybrid работает
- Можно постепенно выносить hot paths в Rust

### [Приоритетные биржи] — 2025-02-22
**Контекст:** Какие биржи поддерживать первыми
**Варианты:** Binance, Bybit, OKX, Kraken, Coinbase, Huobi
**Решение:** Binance, Bybit, OKX
**Почему:**
- Наибольшая ликвидность
- Лучшие API
- Основные для арбитража

### [DEX интеграция] — 2025-02-22
**Контекст:** Нужно ли поддерживать ончейн биржи сразу
**Решение:** Отложить на Phase 2
**Почему:**
- Другая модель (пулы vs orderbook)
- Латентность в секундах (блоки)
- MEV/flashbots — отдельная экспертиза
- CEX проще для валидации архитектуры

## Архив

<!-- Формат: - [Тема](archive/ТЕМА_RESEARCH.md) — однострочный вывод/результат -->
