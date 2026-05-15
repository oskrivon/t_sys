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
Подтверждённые стратегии (после validation pipeline 2026-05-12):

| Стратегия | Статус | Annual | Действие |
|---|---|---|---|
| **Weekend Ensemble** | **LIVE** (Bybit+Binance) | **21.4%** (1x) / **64%** (3x) | Мониторинг, scale через 3 мес |
| Funding capture | Live (обе биржи) | TBD | Ослабить depth фильтр, fix exit slippage |
| Calendar FOMC+Expiry | Confirmed, не имплементирован | ~14% | Следующий приоритет |
| Miro + ML + Vision>=8 | На паузе | +7.3% OOS | H1 scalp dead end (tested 2026-05-15) |
| Volume Ranking L/S | На паузе | +14.2% | Пересмотр через 6 мес |

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
- [x] Weekend Ensemble Strategy:
      **VOTE(KWEB+EWJ+XLK+XLE+USDJPY) → BTC weekend**
      8.6y OOS: Sharpe 2.82, WR 61%, 21.4% annual, profitable 9/9 years.
      
      Фаза 1: ✅ Paper trading + Telegram (cron 21:05 Fri, settle Sun 23:05)
      Фаза 2: ✅ **LIVE** (2026-05-15): Bybit + Binance, dynamic notional (full balance),
        3x leverage, SL 2% conditional на бирже, funding capture paused на выходные.
        Monte Carlo: median $251 за 2.6y (start $66), P95 DD 22%, ruin 0%.
      Фаза 3: scale up при positive results через 3 мес live
- [ ] Weekend Strategy: интеграция с новой инфраструктурой (quant audit 2026-05-13)

      **Контекст:** Weekend strategy живёт в изоляции — собственный SQLite, ручной ccxt,
      cron SL-check раз в 4 часа. После quant audit появились: regime detection, vol-adjusted
      sizing, PortfolioManager с kill switch, backtest cost model. Ничего из этого не используется.

      **Проблема 1: Нет regime detection при входе**
      BTC может быть в VOLATILE режиме (crash/squeeze), а мы входим. SL 2% не спасёт
      при gap-down 5%+ между SL-check'ами. `detect_regime()` уже готов.
      - [ ] В `run_friday_signal()`: вызвать `detect_regime()` на BTC 4h данных
      - [ ] Если VOLATILE (vol_ratio > 2.0) → skip trade + alert "skipped: volatile regime"
      - [ ] Если TRENDING с ADX > 40 → проверить alignment: ensemble direction vs trend_direction
            Counter-trend weekend trade в сильном тренде — опасен
      - [ ] Записать regime в `predictor_details` JSON для post-hoc анализа

      **Проблема 2: Фиксированный размер позиции**
      Одинаковый notional в тихий weekend (BTC ATR 1%) и в volatile (ATR 5%).
      `compute_volatility_adjusted_size()` готов в executor.
      - [ ] Fetch BTC ATR(14) на 4h перед entry
      - [ ] `size = base_notional * (target_vol / actual_vol)`, cap at 2x base
      - [ ] Например: base=$150, target_vol=2%, actual_vol=1% → $300;
            actual_vol=4% → $75 (halved)
      - [ ] Записать actual_vol и adjusted_size в DB для трекинга

      **Проблема 3: SL check раз в 4 часа — gap risk** — ✅ SOLVED
      - [x] Live: conditional stop-loss ордер на бирже (Bybit + Binance)
      - [x] Backup cron poll каждые 4ч + detection exchange-triggered SL
      - [ ] В будущем: WS price stream + instant SL trigger (как funding capture)

      **Проблема 4: Backtest без cost model**
      `validate_weekend_macro.py` считает P&L как `(exit - entry) / entry`.
      Нет fees, slippage, spread. На Bybit futures: 5.5 bps taker × 2 sides = 11 bps RT.
      При avg return +1%/trade это -11% от edge.
      - [ ] В validation script: применить `bybit_futures()` cost preset к Trade objects
      - [ ] Пересчитать net Sharpe и scorecard с реальными costs
      - [ ] Ожидание: Sharpe 1.83 → ~1.6 (всё ещё хороший)

      **Проблема 5: Нет интеграции с PortfolioManager**
      Weekend стратегия не участвует в portfolio risk checks.
      Если funding capture уже at max exposure, weekend может превысить лимит.
      - [ ] Регистрировать weekend как Strategy в PortfolioManager
      - [ ] record_trade_open/close для exposure + drawdown tracking
      - [ ] kill_switch должен блокировать weekend entry тоже

      **Приоритет:** P1 = regime detection (safety), P2 = SL на бирже (safety),
      P3 = vol-adjusted sizing, P4 = cost model в backtest, P5 = portfolio integration

- [x] Risk manager: per-strategy allocation, conflict resolution, drawdown circuit breaker
      (реализовано 2026-05-13: DailyRiskTracker, DrawdownTracker, kill switch в PortfolioManager)
- [ ] Live execution через CCXT (spot для Miro, futures для VR + funding)
- [ ] Начать с $2-5k, scale up при positive results

## TODO

### Vision Scoring: валидация против реальных исходов + решение keep/drop

**Контекст:** Vision scoring (Claude Sonnet via OpenRouter) оценивает графики 1-10.
Текущий статус: используется как фильтр (score>=8), даёт WR 57.8% vs ML-only 31%.
Но эти числа из walk-forward backtest — нет валидации на live/paper trades.

**Проблема:** Мы не знаем:
1. Корреляция vision score с реальными исходами paper trades (а не бэктест)
2. Воспроизводимость — один и тот же график получает разные scores при повторных вызовах
3. Cost/benefit — ~$0.60/мес (текущий объём), но при scale-up?
4. Нет ли у Vision предвзятости (e.g. всегда даёт высокий score trending charts → survivorship)

**План:**

Фаза 1 — Сбор данных (автоматический):
- [ ] В `scanner.py`: при каждом vision-scored signal записывать в SQLite:
      `(symbol, signal_time, vision_score, ml_score, signal_type, regime, outcome)`
      Outcome заполняется позже через paper trading tracker
- [ ] Двойной вызов: раз в 10 сигналов вызывать Vision 2 раза на один chart
      для измерения reproducibility (std между двумя scores)
- [ ] Минимум: 100 сигналов с outcomes (при ~5-10/день = 2-3 недели)

Фаза 2 — Статистический анализ (скрипт `scripts/research/validate_vision.py`):
- [ ] **Rank-biserial correlation** vision_score vs win/loss (робастнее Pearson)
- [ ] **Calibration plot**: P(win | vision_score=1..10) — monotonically increasing?
- [ ] **Reproducibility**: std между дублями, % случаев когда score отличается >2
- [ ] **Conditional value**: vision добавляет alpha поверх ML? Partial correlation
      controlling for ml_score — если partial corr ~0, vision бесполезен
- [ ] **Regime interaction**: vision полезен только в high-vol? low-vol? all?
- [ ] **ROC/AUC**: vision_score как классификатор win/loss (AUC > 0.6 = полезен)
- [ ] **Cost-adjusted edge**: extra edge from vision × trades/month - API cost

Фаза 3 — Решение:
- [ ] Если AUC > 0.65 AND partial corr > 0.1 → **keep**, зафиксировать threshold
- [ ] Если AUC 0.55-0.65 → **conditional keep**, использовать только в high-vol
- [ ] Если AUC < 0.55 OR partial corr < 0.05 → **drop**, убрать из pipeline
- [ ] Записать решение в `docs/RESEARCH.md` → Решения

**Ожидаемый результат:** чёткий ответ "vision добавляет X% edge за $Y/мес"
или "vision — шум, маскирующийся под signal из-за бэктест bias".

---

### [CRITICAL] Пересмотр стратегий по результатам validation pipeline (2026-05-12)

Validation scorecard (`src/validation/`) показал: **ни одна стратегия не прошла все gates.**

**Weekend Effect: ПОДТВЕРЖДЁН (6/7 на 5y, H2-OOS 7/7)**
- 5 лет данных (250 weekends), VOTE(babaF+nqF+techW): N=91, WR=65%, Sharpe=3.43
- CPCV: median 1.84, P(>0)=100% -- PASS
- DSR p=0.019 -- **PASS**
- PBO=0.34 -- **PASS**
- Factor alpha t=3.67 (p<0.001) -- **PASS.** Не объясняется BTC/momentum/size
- Multi-regime: PASS (low-vol 1.47, high-vol 2.64)
- MinBTL: FAIL (need 8y при 202 variants), но при фиксации ensemble n_trials=1 -> PASS
- Walk-forward H1->H2: Sharpe 3.57 -> 2.19, **H2-OOS scorecard: 7/7 PASS**
- **Вердикт: READY FOR PAPER TRADING.** Зафиксировать ensemble, не тюнить.

**Volume Ranking: СЛАБЫЙ EDGE, НЕ СТОИТ УСИЛИЙ**
- На 1 году (51 sym): Sharpe 1.56 -- выглядел хорошо
- На 5 годах (41 sym, expanding universe): **Sharpe 0.63** -- обвалился
- Scorecard 5y (n_trials=1): 5/7 PASS, alpha t=2.06 (p=0.039) -- alpha реален, но слабый
- **CSMB beta упал с 3.10 до 1.53** (не значим на 5y). Не size play на длинной дистанции
- CMOM beta=-2.16 (контрарианен momentum) -- проседает когда momentum работает
- **Multi-regime FAIL**: low-vol Sharpe=1.27, high-vol Sharpe=-0.49 (-10.6% в кризисы)
- Size-neutral variant: Sharpe 1.75 на 1 году, но та же проблема на 5 годах
- Regime filter ненадёжен: 292 переключения за 1799 дней, 68% high-vol эпизодов = 1 день
- **Вердикт: на паузу.** Sharpe 0.63 не оправдывает сложность (daily rebalance, 41 позиция).
  Если вернёмся -- только как компонент портфеля с diversification benefit.

**Miro + ML + Vision: НЕ ПРОХОДИТ**
- CPCV median Sharpe **-0.15** на 5y, P(>0)=39% -- хуже рандома
- Работает ТОЛЬКО в high-vol (Sharpe 1.59), в low-vol убыточна (-0.16)
- Factor alpha t=0.27 -- нет alpha
- **Вердикт: на паузу.** Для реанимации: (1) отфильтровать Vision>=8 subset,
  (2) использовать только в high-vol режиме. Но regime detection ненадёжен.

**Действия:**
- [x] Скачаны 5 лет данных (53 символа, 4h+1d, Binance)
- [x] Weekend: подтверждён на 5y, H2-OOS 7/7 PASS
- [ ] Weekend: зафиксировать VOTE(babaF+nqF+techW), paper trade 3 мес
- [ ] VR: на паузу. Пересмотр через 6 мес с бОльшей выборкой
- [ ] Miro: на паузу. Опционально: проверить Vision>=8 subset (83 trades)

### Funding Capture: анализ limit entry T-5s (risk review)

**Контекст:** entry сдвинут с T-2s на T-5s для limit ордера (3s на fill + fallback market).
Задеплоено 2026-05-11. Нужно проанализировать последствия:

- [ ] Проверить: не увеличивается ли exposure time (3 лишних секунды в позиции до settlement)
- [ ] Оценить risk: rate может упасть между T-5s и settlement (rate re-check в T-2s уже есть, но ордер уже в рынке)
- [ ] Проверить fill rate лимиток по логам после 1-2 дней работы
- [ ] Если limit fill rate <50% — вернуть T-2s и убрать limit entry
- [ ] Сравнить avg slippage до и после (limit vs market baseline)

### Weekend Ensemble → Live (deadline: пятница 2026-05-15 21:00 UTC)

**Контекст:** Paper собирает 1 трейд/неделю, 30 трейдов = 7 мес. OOS Sharpe 1.83, 2 paper trades (1W +1.63%, 1L TBD). Live на $50 даёт те же данные + реальный execution, макс loss при SL 2% = $3.

**Текущая архитектура:** cron-скрипт `scripts/weekend_signal.py` → `src/weekend/runner.py` → paper DB (`data/paper_trades.db`). Нет реальных ордеров — только запись в SQLite + Telegram алерт.

**План:**

Шаг 1 — Execution layer в runner.py:
- [ ] Добавить `live: bool` флаг в `WeekendConfig` (default false)
- [ ] `run_friday_signal()`: после `mark_entry()` → `create_order(BTC/USDT:USDT, market, side, qty)`
- [ ] `run_sunday_settlement()`: `create_order(reduceOnly=True)` → `mark_exit()` с реальной ценой
- [ ] `run_sl_check()`: при SL hit → `create_order(reduceOnly=True)` вместо только записи
- [ ] Leverage: 3x (conservative), notional из конфига (default $150 = $50 × 3x)
- [ ] Exchange: Bybit (уже есть ключи на сервере, BTC/USDT:USDT самый ликвидный)

Шаг 2 — Safety:
- [ ] Qty computation: `notional / btc_price`, round к min_qty биржи
- [ ] Retry logic: если ордер фейлит — 2 retry с 3s delay, потом Telegram alert "MANUAL INTERVENTION"
- [ ] Position check перед entry: если уже есть позиция по BTC — skip
- [ ] Position check перед exit: если позиции нет — skip (already closed)
- [ ] Dry-run guard: `--live` CLI flag обязателен, без него paper-only

Шаг 3 — Config & deploy:
- [ ] `WeekendConfig`: `live`, `leverage`, `notional` поля
- [ ] Обновить cron на сервере: `weekend_signal.py friday --live`
- [ ] Тесты: mock exchange, проверка order flow (entry → SL check → exit)

**Не делаем:**
- НЕ интегрируем в engine/daemon — weekend это 1 трейд/неделю, cron идеально подходит
- НЕ делаем WS мониторинг SL — 4h cron check достаточно (SL 2%, BTC не прыгнет 2% за 4h без новостей)
- НЕ делаем отдельный субаккаунт — $50 на основном Bybit, positions_synced=0 всё равно

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
- [x] Второй docker-compose service `engine-binance` с `ENGINE_EXCHANGE=binance`
- [x] `.env`: добавить `BINANCE_API_KEY`, `BINANCE_API_SECRET`
- [x] Отдельная DB `engine_state_binance.db` и лог `engine_binance.log`
- [x] 4h funding interval normalization (436/602 Binance монет на 4h)
- [x] `defaultType=swap` для scanner (иначе возвращает spot тикеры)
- [x] WS private timeout fix (30min vs 3min)
- [x] Telegram prefix `[binance]`/`[bybit]`

**После Binance:**
- [ ] **Depth filter при scale** — `effective/depth5 <= N` как hard filter (на $25 не работает, при $1k критичен)
- [ ] **Weekend signal -> live execution** — авто open/close BTC perp
- [ ] **Referral Rebate** — -30% к fees при scale up
- [ ] **Threshold tuning** — tier 25-40 bps оптимален, tier 15-25 убыточен, tier 120+ ловушка

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

- **Pairs Trading / Stat Arb** — 14 OOS survivors из 177 пар. Top: BTC/LTC (Sharpe 0.90, WR 68%), DOT/FIL (0.70, 60%), FIL/LTC (0.52, 58%). Portfolio 5 pairs: Sharpe ~1.80, +24.8% annual @5x. Market neutral. Next: cointegration test, paper trading, live engine. Script: `scripts/research/backtest_pairs_trading.py`
- **Range Trading (corridor bounce)** — ML+Vision OOS validated: score>=6 = 200t, WR 38.5%, PF 1.29. LONG score 6-7 = 45.6% WR. Комплементарен к Miro (bounce vs breakout). Next: paper trading для gate, R:R/cost sensitivity analysis. Scripts: `scripts/research/backtest_range_trading.py`, `scripts/research/range_vision_score.py`
- **TG signal channels (pump front-running)** — нужны каналы с императивными сигналами на Binance/Bybit (entry/TP/SL). Найденные каналы — BingX only или отчёты, не сигналы. Приоритет LOW — вернуться при наличии подходящих каналов и $1k+ капитала.
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
