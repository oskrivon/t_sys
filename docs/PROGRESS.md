# Прогресс

## Лог

### 2026-05-19 — Opening Range Breakout: dead end на 4h BTC

**Гипотеза:** range первых N часов сессии (Asian/London/NY/overnight), breakout = direction,
flip при failed breakout (цена вернулась в range).

**Результат:** dead end. Все baseline negative (Sharpe -0.41 до -0.99, WR 34-38%).
Лучший: overnight + tight range + flip 0.3% = Sharpe 0.14, cum +22. Marginal, H2 OOS пустой.

**Почему:** 4h candles слишком грубые для range breakout. BTC 24/7 = нет настоящего "opening".
Direction после breakout = noise на crypto. Может работать на 15m/1h, но данных нет.

**Скрипт:** `scripts/tmp/opening_range_flip.py`

### 2026-05-19 — TradFi Lag: flip не нужен, baseline работает

**Гипотеза:** большое движение NQ (|day_ret| >= 2%), BTC не догнал → enter в direction NQ
после NYSE close (21:00 UTC). Flip если через 4h BTC пошёл против.

**Результат:** flip ухудшает стратегию. Baseline no_flip — лучший вариант.

| Config | N | WR | Sharpe | H2 OOS Sharpe |
|---|---|---|---|---|
| |NQ|>=2.0% no_flip | 98 | 45.9% | **0.73** | **0.74** |
| |NQ|>=2.0% flip=0.7% | 98 | 44.9% | 0.65 | 0.56 |
| |NQ|>=2.0% flip=0.3% | 98 | 42.9% | 0.40 | 0.31 |

**Почему flip вредит:** BTC catch-up растягивается на 8h. В +4h BTC часто ещё не догнал NQ —
это не "wrong direction", а "late arrival". Flip прерывает catch-up процесс.

Sync фильтры (low_sync, btc_quiet) показывают сильный regime shift:
H1 (2021-2023) negative, H2 (2023-2026) WR 73-79%. Не robust для production.

**Вывод:** `|NQ|>=2.0% no_flip` = простая рабочая стратегия. 23 trades/yr, +6.3% annual net,
Sharpe 0.74 OOS. Один cron после NYSE close. Flip = dead end для этого паттерна.
Flip работает только когда confirm window достаточен для determination (weekend 24h = ok, tradfi 4h = too early).

**Скрипт:** `scripts/tmp/tradfi_lag_flip.py`

### 2026-05-19 — Squeeze + Funding Direction: multi-symbol edge found

**Squeeze + momentum/RSI = dead end** (см. ниже). Direction signal = coin flip (WR 37%).

**Squeeze + Funding extreme = работает.** Contrarian к crowded funding + volatility squeeze.
Скачана полная funding history Binance для 19 символов (2019-2026, ~7k records/sym).

Best multi-symbol config: **f>=10bps + flip 0.3%**

| Split | N | WR | Sharpe | Cum |
|---|---|---|---|---|
| Full sample | 41 | 63.4% | 1.18 | +38.1% |
| H2 OOS (Jun 2023+) | 34 | 64.7% | 1.11 | +33.1% |

Flip добавляет Sharpe 0.78 -> 1.18 (+51%) на full sample.
Yearly: profitable 2023 (Sharpe 2.66), 2024 (1.45), 2026 (5.71). Negative 2022, 2025.
~8 trades/year на 18 символах. Высокое quality (avg +0.93%), низкая частота.

Relaxed squeeze threshold (0.7 vs 0.6): +7 trades, OOS Sharpe ~same (1.10 vs 1.11).
0.6 оптимален — строже = чище. ~8-10 trades/year = дополнение к portfolio, не standalone.

**Скрипт:** `scripts/tmp/squeeze_funding_multi.py`

### 2026-05-19 — Squeeze + Flip: dead end без quality direction signal

**Гипотеза:** volatility squeeze предсказывает timing большого движения. Входим по
momentum hint, flip если через N candles losing > X%. Протестировано на BTC 4h (2017-2026).

**Результат:** dead end. Все конфиги отрицательные.

| Вариант | N | WR | Sharpe | Cum |
|---|---|---|---|---|
| Squeeze + momentum (no flip) | 108 | 37.0% | -0.26 | -23.5% |
| Best with flip (cc=6, 1.0%) | 106 | 40.6% | -0.06 | -5.7% |
| Multi-symbol combined | 171 | 36.8% | -0.77 | -69.0% |

Flip mechanism работает (убыток -23.5% -> -5.7%), но direction signal (RSI/momentum) = coin flip.
Ablation OOS: без flip Sharpe -0.77, с flip +0.03 — flip реально помогает, но baseline слишком плох.

**Почему weekend reversal работает а squeeze нет:** weekend имеет quality direction (5-predictor
ensemble WR 60%), squeeze direction = random (WR 37%). Flip не может превратить random в profit.

**Вывод:** squeeze + flip жизнеспособен ТОЛЬКО с quality direction signal (WR >= 55%).
Следующий тест: squeeze + funding extreme direction.

### 2026-05-19 — CI/CD pipeline починен, авто-деплой восстановлен

**Почему CI не деплоил:** две независимые проблемы.

1. **`git diff HEAD~1 HEAD` в multi-commit push** — видел только последний коммит.
   Пуш из 3 коммитов, где #2 менял `src/strategies/` (engine), а #3 `src/weekend/` (scripts) —
   CI видел только #3, engine restart не происходил. Фикс: `github.event.before..github.sha`.

2. **Failing тест блокировал deploy job** — `test_invalid_max_daily_loss_pct[0.5]` падал,
   потому что `Field(ge=0.5)` означает 0.5 = валидное значение. Deploy зависит от test
   (`needs: [test, changes]`), поэтому **ни один пуш не деплоился**. Фикс: тест параметры
   0.5 -> 0.4 (ниже min), 100.1 -> 50.1 (выше le=50.0).

После фикса: CI green, deploy отрабатывает корректно.

### 2026-05-18 — Weekend reversal + funding raw rate fix deployed

**Mid-weekend reversal — OOS validated и задеплоен:**
Анализ intra-weekend price paths на 4h BTC (2017-2026), 127 trades с 5-predictor ensemble.
Если к субботе 21:00 UTC позиция в минусе > 0.3% — разворачиваемся.

| | Baseline | +24h / 0.3% reversal |
|---|---|---|
| H2 OOS Sharpe | 3.35 | **4.41** (+32%) |
| H2 OOS WR | 60.4% | **73.6%** |
| H2 OOS DD | -4.2% | -3.9% |
| H2 OOS Reversals | 0 | 11 / 53 trades (21%) |

Все +24h варианты (0.3/0.5/0.7/1.0%) BETTER на OOS, ни один не WORSE.
Checkpoint Sat 21:00 UTC устойчив: к этому моменту 48% окна прошло,
recovery probability падает до ~40% (vs ~47% на Sat 05:00).

**Реализация:** `check-reverse` команда, cron Sat 21:05 UTC.
Логика: close original + open opposite с новым SL 2%.
Settlement считает combined P&L (leg1 + leg2). Защита от двойного реверса.
DB: `reversal_price`, `reversal_time` columns в `weekend_trades`.

**Funding raw rate fix — задеплоен и подтверждён:**
После ручного деплоя и рестарта engines `above_trade_threshold`:
- Bybit: 4 -> **1** (только RONIN 199bps проходит, BIO 19bps и FIDA 18bps отсечены)
- Binance: 7 -> **5** (ALICE 113bps, RONIN 166bps, FIDA 29bps остались)
CI не подхватил автоматически — пришлось `git pull && docker compose up` вручную.

### 2026-05-18 — Weekend review + два бага в funding capture

**Weekend trade #3:** LONG BTC $79,064 → $77,887 = **-1.49%**.
SL 2% не задет (мин $77,973 vs SL $77,483). Кумулятив 3 трейда: WR 33%, total -2.5%.
Обе биржи открылись корректно в 21:05, settle Вс 23:05.

**Баг 1 — weekend cron retry открывал дубли:**
Cron стреляет 3 раза (21:05/10/15) для надёжности. Retry не проверял наличие
открытой позиции → пытался открыть поверх → `Margin is insufficient` → ложный
алерт "ALL FAILED" в Telegram. Фикс: проверка trade status в DB перед open_position.

**Баг 2 — funding threshold на 8h-equiv вместо raw rate:**
`rate_8h_equiv = raw_rate * (8 / interval_h)` пропускал 1h монеты с raw 5 bps
(8h-equiv 40 bps > порог 25 bps). Per-trade costs (commission + slippage) ~20 bps
не масштабируются с частотой settlement → 5 bps raw = гарантированный убыток.
Данные May 9-18 Binance: 16 из 23 трейдов sub-15bps raw, все net negative.

| Порог (raw) | Трейдов | NET |
|---|---|---|
| >= 0 bps | 23 | -$0.095 |
| >= 15 bps | 7 | +$0.020 |
| >= 25 bps | 4 | +$0.192 |

Фикс: threshold сравнивается с raw rate per settlement.

**Funding Binance итого May 9-18 (excl BTC weekend):**
Funding +$1.25, slippage -$0.77, commissions -$0.78 = **NET -$0.30**.
Maker exit не работает на тонких монетах (0/4 limit fill, все market fallback).
После фикса ожидаем NET positive — только трейды с raw >= 25 bps.

### 2026-05-15 — H1 Scalper Test: D1 levels + H1 entry = dead end

**Гипотеза:** D1 уровни качественные → H1 вход даст больше трейдов при сохранении quality.

**Бэктест:** 12 символов, 8 месяцев, walk-forward. D1 levels + breakout/retest/закол.

| Config | Trades | t/mo | WR | PF | Sharpe | Annual | Exp/trade |
|---|---|---|---|---|---|---|---|
| **D1+4H raw** | 780 | 2.8 | 26.4% | 0.98 | -0.28 | **-2.3%** | -0.016% |
| **D1+1H raw** | 1359 | 15.2 | 25.5% | 0.81 | -0.87 | **-19.2%** | -0.152% |
| D1+1H + ML_d1 thr=0.25 | 1359 | 15.2 | 25.5% | 0.81 | -0.87 | -19.2% | -0.152% |
| D1+1H + ML_d1 thr=0.50 | 1359 | 15.2 | 25.5% | 0.81 | -0.87 | -19.2% | -0.152% |
| D1+1H + ML_h1scalp thr=0.25 | 1359 | 15.2 | 25.5% | 0.81 | -0.87 | -19.2% | -0.152% |
| D1+1H + ML_h1scalp thr=0.50 | 1359 | 15.2 | 25.5% | 0.81 | -0.87 | -19.2% | -0.152% |

**ML не фильтрует:** обе модели (d1, h1_scalp) дают P(win)>0.5 для всех трейдов. Причина:
`level_age` = entry_idx - d1_level.last_idx смешивает индексы разных TF → аномальные фичи.

**Почему H1 хуже:**
- 5.4x больше трейдов, но каждый хуже (exp -0.152% vs -0.016%)
- На H1 цена чаще касается D1 уровня шумом (wick), создавая ложные ретесты
- На 4H шум усредняется — остаются настоящие ретесты
- Даже с идеальным ML нужно отфильтровать 66%+ трейдов и поднять WR на +10pp → нереалистично

**Вердикт:** H1 scalp с D1 levels — dead end. Vision тест не проводился ($6-8 не оправданы).
4H остаётся единственным рабочим entry TF для Miro.

**Скрипт:** `scripts/tmp/miro_h1_d1_backtest.py`

### 2026-05-15 — Weekend Live + Funding Double-Entry Fix

**Funding double-entry баг найден и исправлен:**
CHIP и LAB позиции на Bybit зависли 3+ дней из-за race condition: PostOnly limit fill
мгновенно на thin book, Bybit API лагает → executor не видит fill → шлёт market fallback
→ двойной вход, exit закрывает только половину. Зависшие позиции закрыты: CHIP -$0.95,
LAB +$5.55, net +$4.59. Фикс: проверка status=="closed" + fetch_positions перед market fallback.

**Funding фильтры после ужесточения (12-15 мая):**
Bybit: 45 scheduled → 7 precomputed (15.6%) → 2 signal → 0 executed.
Binance: 23 thin_book reject, 18 precomputed → 1 signal → 1 executed (SIREN +48.5bps).
`min_book_depth_mult: 5` слишком жёсткий — режет 78% кандидатов.

**Weekend стратегия запущена в live:**
- Обе биржи: Bybit + Binance
- Dynamic notional (full balance - 5% reserve), leverage 3x
- SL 2% conditional order на бирже + backup cron poll каждые 4ч
- Monte Carlo validated: 3x даёт median $251 за 2.6y (start $66), P95 DD 22%, ruin 0%
- Kelly: half-Kelly=14%, рискуем ~5%/trade → ниже half-Kelly = conservative
- Cron: Пт 21:05 UTC (сигнал+открытие), Сб/Вс 4ч (SL check), Вс 23:05 (settle)

### 2026-05-14 -- Weekend Strategy: полный re-run на 8.6 годах

Расширен BTC 4h датасет до 2017-08 (было с 2021-06). Скачаны 9000 свечей через Binance API,
merged с existing → 19127 candles, 8.7 лет.

**Результат:** 162 trades, Sharpe 2.82, 21.4% annual (full capital), profitable every year 2017-2026.
Scorecard 6/7 PASS (FAIL только MinBTL при 202 trials). Walk-forward H2 OOS: 7/7 PASS.

**Deep dive findings:**
- Flat BTC week (|ret|<3%) = best: WR 75%, Sharpe 3.72 (vs baseline 61%, 2.82)
- Convergence trade: equity up + BTC down → long weekend = WR 72% (N=32)
- Trailing stop kills the strategy (best conditional: Sharpe 2.11 vs baseline 2.61)
- Saturday dip recovery = myth (dipped >1%: WR 50%, no dip: WR 71%)
- Q1 best (WR 83%), Q3 worst (WR 42%)

---

### 2026-05-13 -- Quant Audit: что было сломано и как починили (7 коммитов)

**Зачем:** Проверили систему глазами кванта — "что бы сказал PhD при code review".
Нашли 20+ проблем от critical до medium. Всё пофиксили за сессию.

---

#### Бэктест врал на 30-50%

| Проблема | Как врал | Фикс |
|----------|----------|------|
| **Look-ahead в swing points** | Алгоритм смотрел на 5 свечей *вперёд*, чтобы найти вершину. В реалтайме будущего нет → бэктест находил уровни, которые в live не видны. | `causal=True`: окно только назад `[i-5, i]`. Старое поведение доступно через `causal=False` для офлайн-анализа. |
| **Gap-through fills** | SL=95, свеча открылась на 92 → бэктест считал fill по 95 (на 3% лучше). | Fill по `min(SL, open)` — как на реальной бирже. |
| **Entry по текущей свече** | Сигнал на закрытии свечи → вход по той же цене. Реально можно войти только на open *следующей*. | `entry_on_next_open=True` по умолчанию. |
| **Entry gap-through** | Open следующей свечи уже ниже SL → бэктест входил и искал выход, вместо мгновенного стопа. | Проверка "open за SL?" → мгновенный SL на entry. |
| **Нет комиссий в weekend бэктесте** | Validation script считал P&L без fees/slippage. 11 bps RT на Bybit = -11% от edge на каждой сделке. | Записано в PLAN: применить `bybit_futures()` cost preset. |

**Результат:** честный бэктест показывает на 30-50% хуже, но это *правда*. Лучше узнать до деплоя.

---

#### Risk management не существовал

| Проблема | Чем грозило | Фикс |
|----------|-------------|------|
| **Нет daily loss limit** | Конфиг `max_daily_loss=10%` нигде не проверялся. Система могла потерять 20% за день и продолжать торговать. | `DailyRiskTracker`: kill switch при -5% за день. Авто-сброс в UTC полночь. |
| **Нет drawdown stop** | Никто не следил за общей просадкой. | `DrawdownTracker`: high-water mark, hard stop при -15% от пика. |
| **Одинаковый размер BTC и PEPE** | PEPE волатильность 15x больше BTC → в 15 раз больше риска при том же размере. | `compute_volatility_adjusted_size()`: размер = risk_usd / ATR%. BTC получает 4.5x больше PEPE. |
| **Exposure не уменьшался** | `current_exposure += size` при открытии, но при закрытии ничего. После 10 сделок risk check бесполезен. | `record_trade_close(size_usd=)` уменьшает exposure. |
| **P&L дрейф (float)** | `total_pnl += 0.123%` — float-ошибки за 500 сделок: ±0.5% в equity. Drawdown trigger срабатывает неточно. | P&L в integer basis points (1 bps = 0.01%). Целые числа не дрейфуют. |

---

#### Execution мог потерять деньги молча

| Проблема | Сценарий "3 ночи" | Фикс |
|----------|-------------------|------|
| **TP/SL без проверки** | Ставим стоп, биржа его отвергает → позиция без защиты, никто не знает. | Retry + проверка order ID + Telegram алерт. |
| **Нет reconciliation** | WebSocket дропнул → TP сработал на бирже, но локально позиция "открыта". Навечно. | Каждые 60 сек: sync с биржей + проверка TP/SL ордеров + force-close stale (>48h). |
| **Partial fill → double entry** | Лимитка частично заполнилась, `fetch_order` таймаут → код думает `filled=0` → шлёт полный маркет → 130% позиции. | 3 retry на fetch. При тотальном провале — считаем полный fill (безопаснее чем дублировать). |
| **Нет heartbeat** | Screener зависает — система молча стоит часами. | Проверка каждые 5 мин, Telegram алерт при 30 мин тишины. |
| **Нет сверки с биржей** | Локальный equity дрейфует от реального баланса. | Каждые 10 мин: fetch баланс биржи, логировать vs локальный. |

---

#### ML модель стагнировала

| Проблема | Почему плохо | Фикс |
|----------|-------------|------|
| **Одна статичная модель** | Рынок меняется, модель обучена на 2024 → в 2026 предсказывает чушь. Нет способа откатить, сравнить версии. | `ModelRegistry`: версионные модели `miro_gb_v{N}.joblib` + JSON metadata. `load_latest()`, `load_version(n)`. |
| **Нет drift detection** | Фичи сдвинулись от тренировочных → модель уверенно предсказывает мусор. | `DriftMonitor`: z-score фичей vs тренировочное распределение. Warning при drift > 2.5σ. |
| **Нет feedback loop** | Модель дала prediction, сделка закрылась — никто не проверил правильность. | `PredictionTracker`: Brier score + rolling accuracy. Warning при плохой калибровке. |

---

#### Regime detection — новая подсистема

**Зачем:** Breakout стратегия зарабатывает в тренде, теряет в боковике. Без фильтра ~30% сигналов — в боковике → убытки.

**Как работает:**
- **ADX > 25** = тренд (торгуем, только по направлению тренда)
- **ADX < 20** = боковик (пропускаем все сигналы)
- **vol_ratio > 2** = кризис/сквиз (пропускаем, слишком непредсказуемо)

**Баг ADX:** Формула Wilder smoothing делила на period дважды → ADX показывал 2-5 вместо 25-80 → фильтр "trending > 25" никогда не срабатывал → 30% мусорных сигналов проходили. Пофикшено.

**Vision scorer:** При падении API score=None → фильтр `if score is not None and score < min` пропускал сигнал. Ночью API упал → все сигналы прошли без Vision. Теперь: fail-closed (None = skip).

---

#### Файлы и где что

| Компонент | Файл | Что там |
|-----------|------|---------|
| Swing points (causal) | `src/strategy/levels.py` | `find_swing_points(causal=True)` |
| Backtest метрики | `src/backtest/metrics.py` | Sortino, Calmar, consecutive losses, universe_note |
| Gap-through + entry_on_next_open | `src/backtest/runner.py` | `simulate_exit()`, `CandleStrategy.run()` |
| Risk management | `src/portfolio/manager.py` | DailyRiskTracker, DrawdownTracker, bps P&L |
| Vol-adjusted sizing | `src/execution/executor.py` | `compute_volatility_adjusted_size()` |
| TP/SL verification + reconciliation | `src/execution/executor.py` | `_place_conditional_order()`, `_reconciliation_loop()` |
| Heartbeat + equity check | `src/execution/executor.py` | `_heartbeat_loop()`, `_check_equity_match()` |
| Regime detection | `src/strategy/regime.py` | `detect_regime()` → TRENDING/RANGING/VOLATILE |
| ML registry + drift | `src/ai/ml_scorer.py` | `ModelRegistry`, `DriftMonitor`, `PredictionTracker` |
| Regime features | `src/strategy/features.py` | `regime_adx`, `regime_efficiency`, `regime_vol_ratio` |
| Regime filter в screener | `src/screener/scanner.py` | Фильтр перед ML scoring |

---

### 2026-05-13 -- Quant Audit: Statistical Sins, Risk Management, Execution, ML Pipeline, Regime Detection

Полный аудит системы "глазами кванта". 3 коммита, 6 подсистем затронуто.

**Statistical Sins (fix):**
- `find_swing_points()`: добавлен `causal=True` — окно `[i-order, i]` вместо симметричного `[i-order, i+order]`. Убран look-ahead bias.
- `get_rolling_levels()`: использует causal mode, убран redundant buffer.
- Backtest metrics: Sortino, Calmar, max consecutive losses, `universe_note` для survivorship bias awareness.

**Risk Management (new):**
- `DailyRiskTracker`: kill switch при daily loss > 5% (was 10%), auto-reset UTC midnight.
- `DrawdownTracker`: equity HWM, hard stop при drawdown > 15%.
- Оба интегрированы в `PortfolioManager._passes_risk_checks()`.
- `compute_volatility_adjusted_size()`: inverse-vol sizing (BTC 4.5x > PEPE при equal risk).
- Paper stats: Sharpe, Sortino, max drawdown, consecutive losses, avg hold.

**Execution Hardening:**
- TP/SL: `_place_conditional_order()` с retry + store order IDs в `LivePosition`.
- Reconciliation loop (60s): sync позиций, re-place пропавшие TP/SL, force-close stale (>48h).
- Event-driven sizing: передаётся `signal.metadata` в `_compute_qty()` (было: exchange minimum).

**Backtest Realism:**
- Gap-through fills: SL fills at candle open when it gaps past stop (e.g. SL=95, open=92 → fill at 92).
- `entry_on_next_open=True` по умолчанию — entry на open следующей свечи.

**ML Pipeline:**
- `ModelRegistry`: versioned models (`miro_gb_v{N}.joblib`), `load_latest()`.
- `DriftMonitor`: rolling z-score vs training distribution, flags drifted features.
- `PredictionTracker`: Brier score + rolling accuracy, warns on poor calibration.
- `ModelMeta`: JSON sidecar с training stats.

**Regime Detection (new `src/strategy/regime.py`):**
- `detect_regime()`: ADX(14), Kaufman efficiency ratio, vol spike → TRENDING/RANGING/VOLATILE.
- Интегрирован в screener: RANGING/VOLATILE → skip, TRENDING → only trend-aligned signals.
- 3 новых фичи в `compute_features()`: `regime_adx`, `regime_efficiency`, `regime_vol_ratio`.

**Осталось:** Vision scoring validation (записано в PLAN.md с развёрнутым планом).

---

### 2026-05-12 -- Validation Pipeline: CPCV, DSR, PBO, Factor Decomposition, Regime Detection

Реализован полный пайплайн статистической валидации стратегий (`src/validation/`):

**Модули:**
- `cpcv.py` -- Combinatorial Purged Cross-Validation (Lopez de Prado 2018). Генерирует C(N,k) путей бэктеста с purging + embargo. Выход: распределение Sharpe, P(Sharpe>0).
- `statistical.py` -- Deflated Sharpe Ratio (DSR), Probability of Backtest Overfitting (PBO), Minimum Backtest Length (MinBTL).
- `factors.py` -- Factor decomposition: регрессия на CMKT, CMOM (2w), CSMB, CARRY. Выделяет alpha vs factor exposure.
- `regime.py` -- HMM regime detection (2-3 state), per-regime Sharpe analysis. Fallback на GMM и threshold если hmmlearn недоступен.
- `scorecard.py` -- Финальный gate: запускает все проверки, выводит scorecard PASS/FAIL.

**Gates (пороги для paper trading):**
1. CPCV median Sharpe >= 0.5
2. CPCV P(Sharpe>0) >= 70%
3. DSR p-value < 0.05
4. PBO < 0.50
5. MinBTL satisfied
6. Factor alpha t-stat >= 2.0
7. Multi-regime profitable

**Тест на реальных BTC данных (719 дней):**
- Factor decomposition: 3 фактора (CMKT, CMOM, CSMB) из 8 альткоинов
- Regime detection (GMM fallback): low_vol 77% дней, high_vol 23% дней
- Scorecard: 7 проверок, полный прогон < 2 секунд

**Результаты валидации всех стратегий:**

| Проверка | Miro+ML+Vision | Vol Ranking L/S | Weekend Effect |
|---|---|---|---|
| CPCV median Sharpe (>=0.5) | -0.22 FAIL | 1.71 PASS | 0.99 PASS |
| CPCV P(Sharpe>0) (>=70%) | 46% FAIL | 71% PASS | 80% PASS |
| DSR p-value (<0.05) | 0.985 FAIL | 0.583 FAIL | 0.535 FAIL |
| MinBTL | 1.1y/6.3y FAIL | 0.9y/3.1y FAIL | 1.9y/2.3y FAIL |
| PBO (<0.50) | skipped | 0.74 FAIL | 0.77 FAIL |
| Factor alpha t-stat (>=2) | 0.14 FAIL | 0.99 FAIL | 0.89 FAIL |
| Multi-regime (>=2) | 2 PASS | 2 PASS | 2 PASS |
| **Итог** | **2/7** | **3/7** | **3/7** |

**Ключевые находки:**
- **Ни одна стратегия не прошла DSR** -- Sharpe не значим после correction за multiple testing
- **PBO>0.5** у VR и Weekend -- вероятность overfitting >50%
- **Factor alpha не значим** нигде -- alpha t-stat < 2.0 у всех трёх
- **Volume Ranking CSMB beta = 3.10*** -- стратегия берёт size risk, а не alpha
- **Weekend: CPCV лучший** (0.99 median, 80% positive) но мало данных (101 trade)
- **Miro: худший** -- CPCV median Sharpe отрицательный, 46% путей убыточны
- **Все прошли multi-regime** -- стратегии работают в обоих режимах волатильности

**Weekend Effect с реальными macro-предикторами (yfinance):**

Загружены 5 лет данных: QQQ, XLK, EWJ, BABA, KWEB, FXI, SPY, DIA, UUP, GLD.
Протестированы 202 стратегии (individual + ensemble combos).

Лучший: VOTE(nqF+techW+japanF) -- N=45, WR=64%, Avg=+1.15%, Sharpe=3.52

| Проверка | Значение | Порог | Статус |
|---|---|---|---|
| CPCV median Sharpe | 2.04 | >=0.5 | PASS |
| CPCV P(Sharpe>0) | 100% | >=70% | PASS |
| DSR p-value | 0.403 | <0.05 | FAIL |
| MinBTL | 2.0y / 8.0y | need 8y | FAIL |
| PBO | 0.688 | <0.50 | FAIL |
| Factor alpha t-stat | 1.94 | >=2.0 | FAIL (borderline, p=0.053) |
| Multi-regime | 2 | >=2 | PASS |
| **Итог** | **3/7** | | |

Walk-forward H1->H2: Sharpe H1=3.72 -> H2=3.28 (edge сохраняется!)
Factor: alpha=+1.88%/yr (t=1.94), R2=0.01 -- стратегия не объясняется known factors.
CMKT beta=-0.0008 (не зависит от BTC direction), CMOM beta=+0.0017 (weak momentum).

**Перезапуск с 5 годами данных (1799 дней BTC, 53 символа):**

Скачаны 5 лет OHLCV свечей с Binance для всех 53 символов (4h + 1d).

**Weekend VOTE(babaF+nqF+techW) на 5 годах -- 6/7 PASS:**

| Проверка | Значение | Порог | Статус |
|---|---|---|---|
| CPCV median Sharpe | 1.84 | >=0.5 | PASS |
| CPCV P(Sharpe>0) | 100% | >=70% | PASS |
| DSR p-value | **0.019** | <0.05 | **PASS** |
| MinBTL | 4.8y / 8.0y | need 8y | FAIL (202 variants) |
| PBO | **0.342** | <0.50 | **PASS** |
| Factor alpha t-stat | **3.67** | >=2.0 | **PASS** (p=0.000) |
| Multi-regime | 2 | >=2 | PASS |

Walk-forward H1->H2: VOTE(babaF+techW+sp500W) Sharpe H1=3.57 -> H2=2.19.
**H2-OOS scorecard: 7/7 PASS. READY FOR PAPER TRADING.**
H2 alpha t=2.24 (p=0.026), multi-regime, DSR p=0.004.

**Volume Ranking на 5 годах:** 2/7 PASS (CPCV ok, PBO=0.56 -- улучшение с 0.74).
**Miro на 5 годах:** 1/7 PASS -- по-прежнему не проходит. Concentrated in high_vol regime.

### 2026-05-12 -- Funding Capture: P&L analysis + optimization

**Baseline (8-12 мая, $35 start -> $34.82):**
| Категория | Сумма | % от funding |
|---|---|---|
| Funding income | +$1.036 | 100% |
| Fees (commission) | -$0.698 | 67% |
| Slippage (realized PnL) | -$0.518 | 50% |
| **Net** | **-$0.180** | **-17%** |

Fees+slippage = 117% от funding. Avg per roundtrip: funding $0.080, fees $0.054, slippage $0.040.
27 сигналов, 13 с funding income. Worst: SPORTFUNUSDT (-$0.265 на $0.034 funding).

**Изменения (deployed):**
1. threshold_bps: 15 -> 25 (отсекаем мелкие трейды не покрывающие fees)
2. max_spread_bps: 5 -> 3 (все 16 rejects были ровно 5.0 -- порог слишком высокий)
3. min_book_depth_mult: 2 -> 5 (тонкий стакан = slippage trap)
4. Limit exit order с 2s timeout + market fallback (экономия ~3bps vs taker)

**A/B мониторинг:** ежедневно в течение недели (13-19 мая) сравнивать net PnL.
Target: net positive per trade (funding > fees + slippage).

### 2026-05-11 — Range Trading: trailing stop, RSI, adaptive trail, volume/BB filters

**Базовая стратегия (4h, 10 символов, OOS TEST):**
Corridor-based: buy support → TP resistance, sell resistance → TP support.
Baseline: 1316t, WR 25.7%, PF 0.93, Sharpe -0.85, Ann -0.1%. Убыточна без фильтров.

**Таймфреймы: 4h — оптимальный.**
- 1h: PF 0.69, Sharpe -6.82 — слишком шумно, overfitting, fees больше % от хода
- 4h: PF 0.93, Sharpe -0.85 — золотая середина
- 1d: 39 трейдов — слишком мало для статистики

**RSI как фильтр — асимметричный результат:**
- LONG + RSI<30: WR 34.5% (29t) — работает, но мало трейдов
- LONG + RSI 30-45: WR 18.6% — хуже baseline (трендовое падение, не капитуляция)
- SHORT + RSI>55: 319t, WR 32.9%, PF 1.29, **Sharpe 1.51** — работает
- SHORT + RSI>70: WR 16.0% — ловушка (пробой сопротивления в тренде)
- Вывод: RSI лучше как фича для ML, не как hard filter. "Перепроданность" у поддержки = momentum down, не mean reversion (кроме extreme RSI<30).

**Trailing stop — главное открытие:**

| Config | N | WR | PF | Sharpe | Annual |
|---|---|---|---|---|---|
| Fixed SL/TP (baseline) | 1316 | 25.7% | 0.93 | -0.85 | -0.1% |
| **Trail 0.5% immediate** | 1316 | **56.9%** | **4.11** | **13.15** | **+0.7%** |
| Trail 1.0% immediate | 1316 | 37.9% | 1.09 | 1.12 | +0.0% |
| Trail 1.5%+ | хуже baseline | | | | |

Trail 0.5% трансформирует стратегию: цена часто идёт 30-70% пути до TP, разворачивается и бьёт SL. Trailing фиксирует эти partial moves. Avg win маленький (+0.005%), но losses ещё меньше.

**Trailing + фильтры:**

| Config | N | WR | PF | Sharpe | Exp/trade |
|---|---|---|---|---|---|
| Trail 0.5% (baseline) | 1316 | 56.9% | 4.11 | 13.15 | +0.005% |
| Trail 0.5% + **Vol spike** | 524 | **64.7%** | **7.63** | 4.46 | **+0.008%** |
| Trail 0.5% + BB + Vol | 230 | 63.5% | 7.18 | 6.61 | +0.007% |
| Trail 0.5% + BB squeeze | 604 | 54.5% | 3.71 | 8.14 | +0.004% |

Volume spike (vol > 1.5x MA) — лучший фильтр: WR 65%, PF 7.6, удваивает exp/trade.
Adaptive trail (% от коридора) НЕ помогает — fixed 0.5% лучше всех адаптивных.

**Trailing + ML + Vision:**
ML и Vision не добавляют value поверх trailing — trailing уже делает все входы микро-профитными.
Fixed SL/TP + ML + Vision>=6 по-прежнему лучший вариант для live (Sharpe 1.59, Ann +6.2%).

**Вывод: trailing 0.5% красив на бумаге (Sharpe 13), но avg trade +0.005-0.008% не покрывает fees (~7-10 bps roundtrip). Для live не viable.** Range trading работает только через fixed SL/TP + ML+Vision фильтрацию, где avg win ~3% компенсирует fees при WR 38%.

### 2026-05-11 — Funding Capture: limit entry, WS fixes, Binance P&L analysis

**Limit order entry (PostOnly/GTX) с market fallback:**
- Entry сдвинут с T-2s на T-5s, limit ордер 3s на fill, fallback market
- Bybit: PostOnly, Binance: GTX (Good Till Crossing)
- **Fill rate: 50%** (2/4 limit залились на Binance: GTC и SONIC)
- Maker fee 2bps vs taker 5bps = экономия 3bps на каждом залитом

**Binance Private WS не доставляет FUNDING_FEE events:**
- Listen key OK, connection OK, но ACCOUNT_UPDATE с FUNDING_FEE не приходит
- Добавлен REST fallback: `/fapi/v1/income` проверка после timeout
- `ensure_private_alive()` в T-10s перед каждым settlement
- Exit timeout пересчитан: settlement_time + 5s (было 30s от entry = 25s лишнего exposure)

**Binance реальный P&L (20 closes, $35 deposit):**

| Тип | $ |
|---|---|
| Funding earned | +$0.875 |
| Price PnL | -$0.211 |
| **Комиссии** | **-$0.611** |
| **Баланс** | **$35.054 (+$0.054)** |

Комиссии 70% от funding income. 5bps taker (VIP0) × 2 стороны = 10bps roundtrip.
При $31 notional: $0.031/roundtrip. Limit entry экономит $0.009/trade (3bps).

**Funding tier analysis (Bybit, 138 trades @ $25, проекция @ $1.5k):**
- 15-25 bps: профитен при $25, убыточен при $1.5k (fees > funding)
- **30-60 bps: единственный профитный tier при $1.5k** (+$3.82/17 дней)
- 100+ bps: убыточен на любом масштабе (WR 47%, slippage-ловушка)
- Cap 100bps сверху — однозначно нужен

**Анализ Васиных +$60/полдня при $1.5k:** подтверждено — удачный день (19 апреля, аномально высокие rates). На 17 днях наших данных та же стратегия при $1.5k = -$167/мес из-за fees 10bps roundtrip.

### 2026-05-10 — Pairs Trading / Stat Arb research: 14 OOS survivors

**190 пар протестировано** (20 символов × C(20,2), 4h, 2 года, split 50/50).
Z-score mean reversion: entry |z|>2, exit z=0, stop z=4, lookback=80 (13 дней).
Fees: 4 ордера × 7bps = 28bps roundtrip.

**14 из 177 пар прошли OOS** (train Sharpe>0.5, test Sharpe>0) — 8% survival rate.

Top 5 OOS survivors:

| Pair | Corr | Test N | Test WR | Test Sharpe | Test PF | Annual @$1k 5x |
|---|---|---|---|---|---|---|
| BTC/LTC | 0.64 | 38 | 68.4% | **0.90** | 1.43 | +31.4% |
| DOT/FIL | 0.80 | 43 | 60.5% | **0.70** | 1.35 | +30.1% |
| FIL/LTC | 0.71 | 31 | 58.1% | **0.52** | 1.32 | +38.5% |
| AVAX/OP | 0.78 | 34 | 67.6% | **0.48** | 1.22 | +20.5% |
| DOT/NEAR | 0.80 | 40 | 62.5% | **0.34** | 1.13 | +11.5% |

**Portfolio (top 5 pairs):** ~10 trades/mo, WR 62.2%, **Sharpe ~1.80**, annual **+24.8%** @$1k 5x.

**Ключевые наблюдения:**
- LTC участвует в 3 из top 5 пар (BTC/LTC, FIL/LTC, OP/LTC) — "якорь" для mean reversion
- Средняя корреляция OOS пар: 0.72 (не экстремальная)
- APT/MATIC и FIL/MATIC — высокий Sharpe, но малый N (3-4 трейда) — ненадёжны
- Market neutral: не зависит от направления рынка

### 2026-05-09 — Binance WS fix, first Binance funding trades, Range Trading research

**Binance WS починен (2 бага):**
- `_handle_message` не распаковывал combined stream format `{"stream":...,"data":{...}}` — все markPrice молча дропались, `_on_funding_update` никогда не вызывался
- `fstream.binance.com` гео-блокирован на сервере — коннект ОК, подписка ОК, данные не приходят. Переключили на `fstream.binancefuture.com` — работает
- Public WS: 0 dead events после фикса (было 180/день)
- Private WS: `exec_funding_exit_timeout` вместо WS `funding_credited` — safety net работает, но 30s лишнего exposure

**CI починен:**
- `test_exit_after_hold` ломался после 2026-05-06 — `mark_entry` использовал `datetime.now()`, тест ожидал фиксированную дату
- `engine-binance` добавлен в CI deploy (раньше деплоился только `engine`)

**Первые Binance funding trades (20:00 UTC):**
- 4 трейда: MITOUSDT (49bps), RAVEUSDT (55bps), COLLECTUSDT, SPORTFUNUSDT
- Полный цикл: entry → precompute → fill → exit (timeout 30s) → positions_synced count=0
- Spread filter отключён для первого теста (temp)
- PnL: в Telegram

**Range Trading research — OOS на 10 символов, 2 года 4h:**

Corridor-based: buy support → TP at resistance, sell resistance → TP at support.
10 символов (BTC, ETH, SOL, BNB, XRP, DOGE, LINK, AVAX, ARB, SUI), split 50/50.

Baseline (без фильтров): 1316 trades, WR 25.7%, PF 0.93 — убыточно.
SHORT стабильно лучше LONG (28.6% vs 23.3% WR) на обеих половинах.

ML filter (GradientBoosting, P>=0.4): 171t, WR 36.3%, PF 1.16.
Top features: `rr` (R:R ratio), `price_change_7d`, `dist_to_sup_pct`.

**ML + Vision (391 trades scored, $3.91 API cost):**

| Score | Trades | WR | Avg PnL |
|---|---|---|---|
| 4-5 | 161 | 21.1% | -0.006% |
| **6-7** | **195** | **39.0%** | **+0.003%** |
| **Score>=6** | **200** | **38.5%, PF 1.29** | **+0.003%** |

LONG score 6-7: **68t, WR 45.6%** — лучший сегмент.
По символам (score>=6): BNB 50%, ETH 43.8%, LINK 42.9%, SOL 41.7%.

**Итоговые метрики (ML + Vision>=6, TEST OOS):**
- 200 trades, WR 38.5%, PF 1.29, **Sharpe 1.59**, annual +6.2%
- LONG only: 68t, WR 45.6%, PF 1.66, **Sharpe 1.98** (лучший сегмент)
- SHORT only: 132t, WR 34.8%, PF 1.10, Sharpe 0.43 (слабый)
- 19 trades/month, avg hold 17h, avg R:R 2.5
- Экономика @$500 10x: ~$2.6/мес, ~$31/год

**Сравнение с другими стратегиями:**
- Weekend effect: Sharpe 1.4, ~14% annual
- Calendar events: Sharpe 1.06, ~14% annual
- Miro + Vision: ~7% annual
- **Range + Vision: Sharpe 1.59, ~6% annual** — zero overlap, portfolio diversifier

**Вывод:** range trading без фильтров убыточен (как и Miro breakout). ML+Vision = Sharpe 1.59, edge реальный. Как standalone слабоват, как дополнение к портфелю — ОК (не коррелирует с другими стратегиями).

### 2026-05-09 — Binance live, funding interval normalization, skip logging, thin_book hard reject

**Binance engine задеплоен и работает:**
- Субаккаунт создан, $35 USDT на futures balance
- `engine-binance` docker service — отдельная DB (`engine_state_binance.db`) и лог (`engine_binance.log`)
- Сканирует 606 USDⓈ-M futures пар (vs 658 на Bybit)
- WS private reconnect-спам пофикшен (timeout 30min vs 3min)
- Telegram уведомления с `[binance]`/`[bybit]` префиксом

**4h funding interval normalization (Binance):**
- 436 из 602 монет на Binance — 4h интервал (6 сеттлментов/день)
- Rate нормализуется к 8h-эквиваленту: `rate * (8 / interval_h)`
- 10 bps на 4h = 20 bps effective → проходит threshold 15 bps
- `fundingInfo` endpoint загружается при каждом скане

**Skip logging в trades_log:**
- `action="skip"` с metadata: reason, funding_bps, book data, spread
- Reasons: thin_book, spread_too_wide, rate_dropped, symbol_not_found, precompute_failed
- `thin_book` стал **hard reject** (раньше warning-only)

**Bybit P&L за 2026-05-08 (первый профитный день):**
- 8 трейдов, WR 87%, NET +0.20 USDT
- Avg slippage -3.1 bps (vs -46.6 bps исторически, улучшение 15x)
- Funding +0.48, Price PnL -0.06, Fees -0.22

**Общий P&L (123 трейда, 15 дней):**
- Funding +14.51, Slippage -13.13, Fees -3.03 = NET -1.65 USDT
- Breakeven analysis: tier 25-40 bps — единственный профитный (+$0.05)
- Монеты с >120 bps funding = ловушка (slippage 200+ bps)

### 2026-05-07 — Binance integration (code ready), dynamic notional, error analysis

**Binance integration — код готов (шаги 1-4 из 5):**
- `BinanceWebSocket` — public @markPrice + private ACCOUNT_UPDATE.FUNDING_FEE, listen key auth (16 тестов)
- Daemon — `--exchange binance`, WS/ccxt factory, legacy compat (15 тестов)
- Strategy scanner — dynamic exchange, Binance `fetch_funding_rates()` (9 тестов)
- Executor — не нужно менять, BinanceWS транслирует в Bybit-совместимый формат
- **Blocked:** ждём Binance аккаунт + API key для deploy (шаг 5)

**Dynamic notional sizing:**
- `target_notional: 0` = engine запрашивает баланс при каждом precompute
- notional = free_balance * 0.9, cap $500, fallback $25 при ошибке API
- При текущих $27: автоматически торгует на ~$24 (13 тестов)

**Исправлены завышенные проекции в документации:**
- $817/мес и +192% annual → помечены как "без slippage, нереалистично"
- Реальность: ~$5/мес Bybit, ~$16/мес Binance при $25 notional
- Max leverage 10x (наблюдённый move 5.96% за hold, ликвидация 20x при 5%)
- Ошибка прогноза 163x: slippage не моделировался + линейный scaling + cherry-picked day

### 2026-05-07 — Funding Capture: full reanalysis (76 trades), spread filter, Binance depth

**Reanalysis на полных данных (76 trades с book data, не 29 как считали):**
- `trades_log_server.json` содержит 201 записей, 153 с book_t2s (ранее анализировали только 50 из CSV)
- 76 paired open-close trades с book snapshots (Apr 24 — May 5)

**Spread filter — OOS-валидирован:**
- Хронологический split 50/50 (38+38 trades)
- `spread < 5 bps`: TRAIN avg=+$0.040, WR=54% | TEST avg=+$0.035, WR=70% — **YES**
- `spread < 3 bps`: TRAIN avg=+$0.047 | TEST avg=+$0.078 — **YES** (но N=13 на TEST)
- `spread >= 7 bps` и выше: не проходит OOS (avg < 0 на TEST)
- Внедрён `max_spread_bps: 5.0` в engine — reject при spread >= 5 bps

**Depth filter — не работает при $25, критичен при scale:**
- При $25 (eff $250): median ratio = 0.32x от L5 depth → книга не фактор
- При $1k (eff $10k): 95% trades превышают L5 depth, median ratio 12.9x
- Вывод: depth filter бессмысленно валидировать на $25, нужен при scale

**Binance vs Bybit depth comparison (51 общая монета, live orderbook):**
- Binance глубже: median 2.8x (L5), 3.3x (L20); max до 107x (APE, KNC, SENT)
- При $1k notional: Bybit fit_L5=8%, Binance fit_L5=29%, **Binance fit_L20=78%**
- При $5k: Binance fit_L20=31% — потолок ~15 монет
- Spreads: примерно равны (median ~3 bps обе биржи)
- Fees: Binance 8 bps RT vs Bybit 11 bps RT — экономия $3/trade при $1k

**Bybit-only монеты** (5 штук: AIOZ, BOBA, GIGA, GODS, TSTBSC) — ультра-тонкие L5 $43-$367, только для $25 notional.

### 2026-05-06 — Инфраструктура: watchdog kill-loop, CI deploy fix, /status heartbeat

**Engine рестартился каждые 5 минут:**
- Причина: `watchdog_engine.sh` (cron `*/5 * * * *`) проверял `/tmp/engine_heartbeat` на хосте, но engine пишет его внутри контейнера → файла нет → `pkill -9`
- Фикс: удалён watchdog из crontab, убиты ghost-процессы на хосте
- Docker `restart: unless-stopped` достаточен для recovery

**CI/CD не деплоил telegram-bot и paper-trading:**
- `src/api/telegram/` и `src/paper_trading/` отсутствовали в change detection
- `src/core/` ребилдил только engine, хотя это shared-код
- Фикс: `src/core/` → ребилд всех; добавлены telegram-bot и paper-trading как отдельные deploy targets

**Paper-trading падал при старте:**
- `ImportError: cannot import name 'format_stats'` — ссылка на удалённую функцию
- Фикс: убран лишний импорт из `service.py`

**Новый /status с Redis heartbeat:**
- Каждый сервис пишет `heartbeat:<name>` в Redis (TTL 90s, интервал 30s)
- `/status` в TG-боте показывает: [OK]/[SLOW]/[DOWN] для всех 6 сервисов + engine details + paper stats
- Не требует Docker socket внутри контейнера

### 2026-05-05 — Screener fixes + Volume Ranking paper + Daily Digest

**Screener breakout persistence fix:**
- Breakout state хранил DataFrame idx, который терялся при рестарте контейнера (idx сдвигался)
- Теперь breakouts ремапятся через `candle_ts` → корректный idx после рестарта
- ML threshold снижен: 4h 0.25→0.15, 1h 0.40→0.20
- Backtest: 200 сигналов/35d (WR 49%) vs 105 при старом threshold (WR 53%)

**Volume Ranking запущен в paper mode:**
- Включена в engine (`config/strategies.yml`), scheduler запускает daily в 00:05 UTC
- Targets логируются в `engine_state.db` как `paper_target` (без реальных ордеров)
- 50 монет, top 50% long / bottom 50% short по volume acceleration (7d/30d)

**Daily Digest в Telegram:**
- Cron 08:00 UTC → `scripts/daily_report.py --telegram`
- Docker health, Redis, engine restarts
- Funding: 24h trades, WR, funding earned, staleness warning
- Screeners: scans, alerts, ML filtered
- Volume ranking: paper targets

**Redis DNS fix:**
- Redis был запущен вне compose (сеть `bridge` вместо `trading_default`)
- Скринеры не резолвили DNS-имя `redis` → fallback без Redis
- Пересоздан через compose → скринеры подключились, сигналы идут в Redis

**Maker/Limit exit analysis — REJECTED:**
- Limit exit fill rate всего 18% (цена идёт против нас в 82% случаев)
- Экономия +1.4 bps/trade — шум при adverse move -50 bps
- Maker entry хуже: раннее размещение (+8 сек exposure) добавляет риск drift
- Вывод: оптимизация fee бессмысленна при hold 2-30 сек, фокус на фильтрации и Binance

**Найденные проблемы при аудите (все исправлены):**
- Engine рестартовался 8 раз за сутки (watchdog каждые 5 мин)
- Screeners 0 сигналов за 11 дней (breakout state + ML threshold) → fixed
- Redis был down у скринеров (DNS) → fixed
- `trades_log_server.json` не обновлялся 9 дней (данные были в engine_state.db)

### 2026-05-05 — Funding Capture: Depth/Spread Analysis (76 trades OOS)

**Данные:** 76 closes с book_t2s (22 Apr - 5 May), engine_state.db на сервере. Локальная копия: `data/trades_log_server.json`.

**P&L breakdown ($25 notional, 10x lev):**
- Funding earned: +$425 (avg $5.59/trade)
- Slippage: -$394 (avg -$5.18/trade)  
- Fees (11bps RT): -$84
- NET: -$52 total

**Корреляции:**
- `spread_bps vs slippage`: r=-0.47, p<0.0001 — **достоверно**
- `depth5 vs slippage`: r=0.08, p=0.50 — NOT significant (мало данных)

**Train/Test split (38/38):**
- Фильтр на train: spread<5 + depth5>$1000 → avg +$0.77/trade
- На test: avg -$0.07 (breakeven), WR 71%. Не cherry-pick, но и не profit.
- Rejected trades (spread>9): -$54 из -$52 total loss. Фильтр отсекает убытки достоверно.

**Доверительные интервалы:**
- Best filter (spread<5, depth5>$1000): mean=$0.77, 95% CI [-$0.10, +$1.63], p=0.08
- Bootstrap P(mean>0) = 97%, но формально not significant
- Cohen d = 0.34 → нужно **~70 filtered trades** (34 дня) для 80% power

**Масштабирование до $100 notional:** НЕ рекомендуется.
- 83% трейдов: order > L1 depth (median L1 = $42)
- Slippage вырастет нелинейно при ratio > 1x
- Безопасных трейдов всего 11/76

**Решение:** hard filter `spread >= 9 bps` → reject. Продолжаем собирать данные. Масштабирование через Binance (deeper books), не через увеличение size на Bybit.

### 2026-05-02 — Counter-Funding Mean Reversion: DEBUNKED (directional bias)

**Initial result:** WR 82%, net +0.95%/trade, 25 стратегий прошли walk-forward. Слишком хорошо.

**Sanity checks выявили:**
1. **100% short bias** — все 82 трейда при >50bps были SHORT. Extreme funding на micro-cap всегда отрицательный (shorts платят в pump). "Counter" = SHORT = шорт после сквиза.
2. **Не mean reversion, а continuation** — цена НЕ разворачивается. При negative funding (pump) цена продолжает падать после settlement. WR "reversal" = 17% (т.е. 83% continuation).
3. **H2 деградирует** — SHORT-only walk-forward: H1 net +0.23%, H2 net +0.03%. С учётом slippage 0.25% — убыточно.
4. **Permutation test прошёл** (p=0.0000, 3.9 sigma) — edge реальный, но это directional bias, не structural.
5. **LONG сторона не проверена** — N=0 при >50bps positive funding. В бычьем рынке стратегия может не работать.

**Вывод:** красивые числа — артефакт bearish micro-cap режима Feb-Apr 2026. Не structural edge. Закрыто.

**Continuation dump:** тоже проверен — ALL FAIL. "Continuation" = зеркало counter-trade, та же directional bias наоборот.

**"Sell to bots" (enter T-5m, exit T-1m close):** ALL FAIL. Pre-settlement drift = 0% (шум). Рынок не anticipates settlement.

**Ключевая находка — minute-by-minute drift при >50bps:**
- T-5m...T-2m: ~0% (random walk, no anticipation)
- T-1m: **+0.14% WR 68%** — боты реально двигают цену в последнюю минуту
- T+0m: **-0.67% WR 17%** — settlement dump (directional bias, не structural)

Bot impact (+0.14%) реальный, но **меньше fee+slippage (0.26%)**. На минутных данных untradeable. Для эксплуатации нужен sub-second entry за 10-30 сек до settlement → colocation territory.

**Урок:** permutation test + walk-forward недостаточны без direction split. Всегда проверять % long vs short и тестировать стороны отдельно.

### 2026-05-02 — Three bot-exploit strategies research

**A. Post-Listing Dump (13 Bybit listings, Sep 2025 — Apr 2026):**
- Avg spike: +12% (range 0-20%), peak обычно на 5-55 мин
- Short@10min→30min: avg +2.5%, но N=13 и WR баг. Частота ~22/year — мало.
- В бэклог: нужен парсер анонсов + больше данных.

**B. Round-Number Level Exhaustion (2209 round vs 972 non-round breaks):**
- Round breaks: follow-through WR 51% stable через 4h/8h/12h
- Non-round breaks: WR 43-47%, decays to 43% at 8h
- Grid боты "тормозят" round-number пробой (break size 1.09% vs 1.27%), но когда пробивает — continuation лучше
- Actionable: добавить `level_is_round` feature в Miro ML. Low priority.

**C. MM Spread Widening (56 book snapshots at T-2s):**
- Spreads 5-25bps перед settlement vs нормальные 1-2bps — MMs уходят
- Идея: limit orders в расширенный спред как temporary MM
- Нужна orderbook WS инфра для real-time monitoring. В бэклог.

**Скрипты:** `scripts/tmp/three_bot_strategies.py`

**Скрипты:** `scripts/tmp/counter_sanity_check.py`, `scripts/tmp/microcap_frontrun_analysis.py`

### 2026-05-02 — Counter-Funding Mean Reversion: initial result (SUPERSEDED — see above)

**Гипотеза:** боты создают предсказуемый price impact вокруг funding settlement. Можно ли торговать against/with them?

**Данные:** 4045 settlements × 17 micro-cap монет × 3 месяца (Feb-Apr 2026), 1m candles T-5m...T+5m, funding rates через Bybit API.

**Результат front-run ботов (with-bot): FAIL на всех конфигурациях.**
- На liquid монетах (BTC, ETH, SOL): 3780 settlements — нет drift, нет edge
- На micro-cap: with-bot стратегия убыточна (WR 22-38%)

**Результат counter-bot (OPPOSITE to funding direction): CONFIRMED OOS!**

Walk-forward H1/H2, 25 стратегий прошли:

| Стратегия | Thr | H1 net | H2 net | H2 WR | H2 N | Est $/мо на $250 |
|---|---|---|---|---|---|---|
| Counter T-1m→T+2m | >50bps | +0.51% | +0.95% | 82% | 61 | $97 |
| Counter T0→T+1m | >50bps | +0.73% | +0.69% | 79% | 61 | $71 |
| Counter T-1m→T+1m | >50bps | +0.39% | +0.64% | 79% | 61 | $65 |
| Counter T-1m→T+2m | >20bps | +0.61% | +0.48% | 70% | 144 | $115 |

**Механизм:** extreme funding = extreme positioning (все на одной стороне). Settlement триггерит mean reversion — цена откатывает в сторону, противоположную crowded trade. Не бот-front-running, а структурный mean reversion.

**6 consistent монет (H1>0 И H2>0 при >10bps):**
- SIREN: 70 trades, WR 80%, net +0.43%, avg_fund 50.7bps
- SOON: 14 trades, WR 100%, net +1.01%, avg_fund 35.8bps
- GODS: 7 trades, WR 100%, net +1.05%, avg_fund 59.5bps
- ORCA: 10 trades, WR 80%, net +0.38%, avg_fund 57.3bps
- ESP: 14 trades, WR 71%, net +0.18%, avg_fund 30.3bps
- ENSO: 33 trades, WR 65%, net +0.15%, avg_fund 36.7bps

**Совместимость с текущим funding capture:** можно делать оба — сначала собрать funding credit, потом counter-trade на reversal. Двойной доход с одного settlement.

**Caveats:**
- N=61 для >50bps — OK, но не огромная выборка
- Per-coin N маленькие (7-70), нужно больше данных
- Micro-cap liquidity: slippage при $250+ notional не проверен
- Нужен live test 2-4 недели перед scale up

**Скрипты:** `scripts/tmp/frontrun_bots.py`, `scripts/tmp/download_microcap_fast.py`, `scripts/tmp/microcap_frontrun_analysis.py`
**Данные:** `data/reports/microcap_settlements.csv` (4045 settlements), `data/reports/settlement_candles_3m.csv` (4991 settlements liquid coins)

### 2026-05-02 — Funding capture live: 78 trades, 10 days, ~breakeven

**Engine live stats (Apr 22 — May 2):**
- 78 trades, 7.8/day, 46 unique micro-cap coins
- Price-only PnL: -32.7% (slippage доминирует)
- Net PnL (price + funding - fees): **-1.1%** (~breakeven)
- Net WR: 50%, profitable days 4/10
- Avg funding: 51.5 bps, but avg price impact: -42 bps → funding почти полностью съедается

**Book depth analysis (56 trades with data):**
- Deep books (Q4, $38k): net +0.11% vs thin books (Q1, $391): net -0.04%
- Spread >5bps → worse results (r=-0.157)
- N=56 недостаточно для robust фильтра, нужно 100-150+

**Weekend signal: первый live trade** — LONG BTC $77,868 (May 1), 4/5 predictors, PnL +0.48% к моменту анализа.

**Вывод по scale-up:** $300-400 на Bybit достаточно для всех стратегий (leverage позволяет). $500 yolo = разумный micro-live test. НО сначала: depth filter, weekend auto-execution, counter-trade integration.

### 2026-04-29 — Portfolio estimate: weekend + calendar stack ~25-27% annual

**Combined strategy stack (без funding capture, на $10k):**

| Стратегия | Trades/yr | Avg net/trade | Annual contribution | Capital use |
|---|---|---|---|---|
| Weekend 5-WAY | 18 | +0.86% | +16.8% | 10% времени |
| FOMC pre-LONG | 8 | +0.57% | +4.6% | 0.7% |
| FOMC post-SHORT | 8 | +0.65% | +5.3% | 2.2% |
| Q-expiry SHORT | 4 | +0.78% | +3.2% | 1.1% |
| **Total** | **38** | — | **~30% gross** | **~14%** |

С поправкой на корреляцию (~10-15%): **~25-27% net annual**. Капитал занят 14% времени, остальные 86% свободны.

**Сравнение с хедж-фондами:** по % return = top 10-25% HF (Citadel/Two Sigma territory). По Sharpe 1.2-1.5 = top 25%. Но: scale $10k vs $10B, один актив (BTC), 3-4 паттерна, 0 live track record. Первые 6-12 мес live покажут реальность edge.

### 2026-04-29 — Calendar Events: FOMC drift + Quarterly expiry dump (NEW STRATEGY)

**Три calendar-паттерна протестированы:**

**1. Pre-FOMC LONG (-8h to decision): CONFIRMED OOS**
- BTC растёт перед FOMC (positioning risk-on в ожидании clarity)
- Full: N=39, WR 69%, avg +0.71%, Sharpe_net 0.75
- Walk-forward: H1 WR=68% avg=+0.88% | **H2 WR=70% avg=+0.56%** (consistent!)
- Entry: FOMC day 10:00 UTC, Exit: 18:00 UTC. 8 раз/год. Без SL (MAE -1.07% median).

**2. Post-FOMC SHORT (+24h): PARTIAL**
- BTC падает после FOMC (profit-taking после clarity)
- Full: WR 59%, avg +0.80%
- Walk-forward: H1 WR=68% avg=+1.03% | H2 WR=50% avg=+0.58% (слабее на H2)

**3. Post-Quarterly-Expiry SHORT (+24h): CONFIRMED OOS**
- BTC падает после квартального опционного expiry (pin release + rebalancing)
- Full: N=24, WR 58%, avg +0.93%, Sharpe_net 0.58
- Walk-forward: **H1 WR=58% | H2 WR=58%** (identical — very stable)
- Year-by-year: 5/6 лет profitable
- Entry: expiry Friday 08:00 UTC, Exit: +24h. 4 раза/год. MaxDD -4.7%.

**Calendar Portfolio (combined): Sharpe_net 1.06, ~14% annual, 20 trades/yr**

Не пересекается с weekend signal по времени → стекается на тот же капитал.

**Скрипты:** `scripts/tmp/macro_events_btc.py`, `scripts/tmp/fomc_expiry_deep.py`

### 2026-04-29 — Big equity move -> BTC lag: intraday research (WEAK EDGE)

**Гипотеза:** если NQ сильно двигается (>=2%), BTC реагирует с лагом 4-8 часов после NYSE close. Можно ли обобщить weekend effect на будни?

**Результат: частично подтверждено, но edge слабый.**

Основной анализ (NQ daily 5y + BTC 1H 5y):
- NQ >=2.0% -> BTC +8h: WR 56%, avg +0.43%, **Sharpe_net 0.61** (OOS consistent: H1 0.69, H2 0.66)
- NQ >=3.0% -> BTC +4h: WR 69%, avg +0.39% (N=29, мало)
- Annual ~6% net, MaxDD -10% — слабее weekend effect в 3-4 раза

Late session moves (NQ 1H, 2y): WR 88-100% при NQ>=1.5% late, **но N=8** — недостаточно для статистики.

**Sync-прокси (BTC не отреагировал) — НЕ РАБОТАЕТ:**
- counter_move (BTC пошёл против equity) — антисигнал, H1 Sharpe -2.4
- low_sync (<30%) — нестабильный между режимами (H1 -2.1, H2 +0.7)
- btc_quiet (low range) — не добавляет информации

**Почему будни слабее выходных:** в будни BTC liquidity нормальная, маркет-мейкеры работают, mean reversion быстрая. Weekend liquidity vacuum — ключевой фактор edge. Без него lag "не пролонгируется".

**Пятница vs будни при NQ>=1.5%:**
- Friday: avg_net **+0.42%** (weekend effect)
- Non-Friday: avg_net **-0.09%** (нет edge)

**Вывод:** weekend effect — уникальный structural edge, обобщение на будни не работает. NQ>=2.0% standalone = 6% annual (не оправдывает отдельную стратегию). Но как подтверждение пятничного фильтра — Friday NQ>=2.0% усиливает weekend signal.

**Скрипты:** `scripts/tmp/bigmove_tradfi_lag.py`, `scripts/tmp/bigmove_sync_proxy.py`

### 2026-04-29 — Новые категории предикторов: Forex, Alt/BTC, CME gap, Commodities

**Тестировали:** 45 сигналов из 5 новых категорий + reference equity. Walk-forward H1/H2, correct annualization.

**Новые предикторы, прошедшие OOS:**
- **usdjpy_week** (FOREX) — H2 Sharpe_c +0.78. Carry trade = global risk appetite proxy. Лучший из forex.
- **solbtc_fri** (ALT/BTC) — H2 Sharpe_c +0.43. Альткоин-ритейл в пятницу = weekend sentiment.
- **copper_fri** (COMMODITY) — H2 Sharpe_c +0.40. Dr. Copper работает.
- **copper_miners_fri** — H2 Sharpe_c +0.54.
- **cme_btc_fri** (CME) — H2 Sharpe_c +0.14. Слабый соло, но хорош в ансамблях.
- **tips_week** (BOND) — H2 Sharpe_c +0.50.

**Не работают:** EUR/USD, GBP/USD, AUD/USD, NZD/USD, USD/CNY, ETH/BTC, CME gap (спот vs фьючерс), gold/silver ratio, TIP/TLT ratio.

**Кросс-категорийные ансамбли: 30/30 прошли OOS!**

Топ-5 по H2 Sharpe_net:
1. VOTE(solbtcF+nqF+energyW) — **Sn=1.44**, avg +1.04% — бьёт оригинал!
2. VOTE(usdjpyW+nqF+techW) — Sn=1.26
3. VOTE(usdjpyW+copper_minersF+techW) — Sn=1.26
4. VOTE(usdjpyW+techW+japanF) — Sn=1.14
5. VOTE(usdjpyW+cme_btcF+techW) — Sn=1.10

Оригинальный VOTE(babaF+nqF+techW) Sn=1.40 — всё ещё в топе.

**Вывод:** USD/JPY (carry trade) и SOL/BTC (крипто-ритейл sentiment) — два сильнейших новых источника сигнала. Добавляют diversity к equity-only предикторам.

**Скрипт:** `scripts/tmp/weekend_new_predictors.py`

### 2026-04-29 — Walk-Forward OOS + скорректированные метрики (CONFIRMED, Sharpe net 1.4)

**Методология:** разделили 253 выходных пополам. H1 (2021-05 — 2023-10, 126 wk) — выбор предикторов. H2 (2023-10 — 2026-04, 127 wk) — слепая проверка ТОЛЬКО выбранных. Никакого подглядывания.

**Индивидуальные предикторы:**
- 23/33 прошли OOS (70% survival rate)
- Лучший OOS: **china_inet_fri** (KWEB) — H1 Sharpe 0.85 -> H2 Sharpe **1.35** (вырос!)
- **japan_fri**: H1 0.96 -> H2 **1.25** (тоже вырос)
- **tech_week**: H1 1.46 -> H2 **1.00** (удержался)
- Провалились: sp500_week, oil_week, googl_week, baba_week, silver_week, meta_week, aapl_week

**Ансамбли:**
- **18/20 прошли OOS** (90% survival!)
- Лучший OOS: VOTE(MSFT_week + BABA_fri + Energy_week) — H2 Sharpe raw 2.90, **correct net 1.17**
- Median Sharpe retention H2/H1: 25.7% (ожидаемо — H1 Sharpe раздуты селекцией)

**Оригинальный VOTE(BABA_fri + NQ_fri + XLK_week) — скорректированные метрики:**

|  | Sharpe raw (sqrt52) | Sharpe correct (sqrt N/yr) | Sharpe net | Avg/trade net | Annual net |
|---|---|---|---|---|---|
| FULL (90 tr, 5yr) | 2.87 | 1.70 | **1.44** | +0.86% | **+16.8%** |
| H1 in-sample (47 tr) | 2.85 | 1.73 | 1.49 | +0.95% | +19.9% |
| H2 out-of-sample (43 tr) | 2.95 | 1.70 | **1.42** | +0.76% | **+14.0%** |

- MaxDD (net): **-9.5%**
- Капитал занят ~10% времени (48ч × 18 раз/год) — остальные 90% свободны

**Источники инфляции Sharpe (2.9 -> 1.4):**
1. sqrt(52) -> sqrt(18): торгуем 18 раз/год, не 52 — главный фактор (×0.59)
2. Transaction costs 0.15%/trade (taker ×2 + spread + slippage) — съедают ~15% дохода
3. N=43-90 трейдов — CI для Sharpe ±0.5-0.8

**Вывод:** Sharpe net 1.4, годовая ~14-17% при DD -9.5%. Edge реальный и подтверждён OOS. Стратегия капиталоэффективна (90% времени капитал свободен).

**Скрипт:** `scripts/tmp/weekend_walkforward.py`

### 2026-04-27 — Weekend Ensemble: поиск предикторов (32 тикера × 2 сигнала)

**Поиск предикторов:** 32 тикера × 2 сигнала = 64 комбинации протестировано.

**Сюрпризы:**
- **Alibaba fri** (#1 single) — бьёт NASDAQ. Азиатский risk sentiment.
- **Japan fri** (#2) — японский ритейл активен на выходных.
- **China Internet fri** (#4) — улучшается во второй половине выборки.
- Crypto-adjacent (MSTR, COIN) — посредственно. Слишком коррелированы с BTC.
- Oil, Gold, VIX — не работают или обратные.

**Ensemble VOTE:** торгует только когда 2+ из 3 independent предикторов согласны. Фильтрует шумные сигналы. ~18 трейдов/год, profitable 6/6 years (включая 2023).

**NB:** Raw Sharpe в этой записи был завышен (2.82) из-за sqrt(52) аннуализации — см. скорректированные метрики выше.

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
- При $1k/10x: +$9.08 per settlement, ~$817/мес **(теоретический max БЕЗ slippage — см. UPDATE 2026-05-07)**
- При $10k/max lev: +$190 per settlement, ~$17k/мес **(нереалистично — slippage съедает ~90%)**
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
| Funding capture spread<5bps | ~$5-14/мес@$25 Bybit | WR 60-70% | Live, spread filter deployed |

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
