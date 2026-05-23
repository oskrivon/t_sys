# Weekend SL + V-Bottom Re-Entry Research

**Дата:** 2026-05-23
**Статус:** DEPLOYED (2026-05-23)

## Проблема

Текущий weekend SL (2%) теряет слишком много total PnL:
- Без SL: +75.29%, Sharpe 1.74, MaxDD -9.19%
- С SL=2%: +60.41%, Sharpe 0.92, MaxDD -5.73%
- 51% стопов — ложные (цена восстанавливается)

## Исследование

### Этап 1: Post-SL Recovery (dead end)
Тестировали: после SL hit, восстанавливается ли BTC?
- Recovery rate 40% (coin flip), flip в SHORT тоже не работает
- Re-entry без bounce detection ухудшает Sharpe
- **Вывод:** слепой re-entry не работает

### Этап 2: SL Prediction
Тестировали pre-entry фичи (127 trades, 35 SL hits):
- **Friday range (p=0.007)** и **trend strength (p=0.001)** — статистически значимы
- Лучший фильтр: `ret_14d < -3%` → Sharpe 2.57, но total PnL падает -3%
- Counter-trend на большой выборке не подтвердился (был шум на малой)
- **Вывод:** фильтрация улучшает Sharpe но режет total PnL

### Этап 3: Tighter SL
SL=1.0% даёт total +117% vs SL=2% +96% (на raw PnL без текущего clipping)
- Причина: 62% стопов ложные — тighter SL меньше теряет на каждом

### Этап 4: V-Bottom Re-Entry (прорыв)
После SL hit, ждём bounce X% от локального дна, затем re-entry:

| SL | Bounce | Re-SL | Total PnL | Sharpe | MaxDD |
|---|---|---|---|---|---|
| current 2.0% | none | - | +60.41% | 0.92 | -5.73% |
| 0.75% | 0.5% | 1.0% | **+81.87%** | **2.13** | **-4.68%** |
| 1.0% | 0.5% | 1.0% | +78.75% | 2.02 | -5.54% |
| no SL | - | - | +75.29% | 1.74 | -9.19% |

### Этап 5: Bottom Detection Methods
Сравнили 6 детекторов дна:

| Метод | Detect rate | Re-entry WR | Sharpe |
|---|---|---|---|
| **Simple bounce 0.5%** | 71/73 | **58%** | **1.97** |
| Composite (bounce+struct) | 70/73 | 61% | 1.95 |
| Higher low (3 candles) | 68/73 | 56% | 1.60 |
| Momentum (3 green) | 59/73 | 47% | 1.38 |
| Volume spike | 49/73 | 51% | 1.05 |
| Consolidation 3h | 28/73 | 57% | 1.06 |

**Simple bounce — лучший.** Структурные паттерны не дают edge, Vision не нужен.

Timing: re-entry до 16h после SL = 64% WR. После 24h — не стоит.

### Этап 6: LONG vs SHORT Asymmetry
Re-entry работает для обоих направлений:

| Direction | Re-entry WR | Base Sharpe | +Re-entry Sharpe | ΔTotal |
|---|---|---|---|---|
| LONG (V-bottom) | 54% | 0.66 | 1.63 | +18.36% |
| **SHORT (pullback)** | **62%** | 1.37 | **2.44** | +22.56% |

SHORT re-entry ещё лучше — weekend пампы на тонкой ликвидности чаще откатываются.

## Рекомендованная конфигурация

```
SL = 0.75%        # (было 2.0%) — tighter, быстро выходим
bounce = 0.5%     # триггер re-entry: bounce 0.5% от локального дна
re_entry_sl = 1.0% # SL на re-entry позицию
max_reentry_h = 16 # не входить если прошло >16h после SL (WR падает)
```

## Квант-валидация (полный scorecard)

127 trades, 5.3 лет, 7 вариантов стратегии.

### Performance comparison

| Вариант | Total PnL | PnL/yr | Sharpe | MaxDD |
|---|---|---|---|---|
| SL=2% (old) | +61.04% | ~11.5%/yr | 1.48 | -13.3% |
| **SL=0.75%+reentry** | **+80.14%** | **~15.1%/yr** | **2.09** | **-4.7%** |
| No SL | +75.92% | ~14.3%/yr | 1.75 | -9.2% |

### Scorecard (7 tests)

| Тест | SL=2% (old) | **SL=0.75%+reentry** | No SL |
|---|---|---|---|
| CPCV median Sharpe | 1.00 ✅ | **1.34** ✅ | 1.10 ✅ |
| CPCV P(Sharpe>0) | 100% ✅ | **100%** ✅ | 100% ✅ |
| **DSR p-value** | 0.187 ❌ | **0.016** ✅ | 0.094 ❌ |
| MinBTL | 5.35y ✅ | **5.35y** ✅ | 5.35y ✅ |
| PBO | 0.112 ✅ | **0.112** ✅ | 0.112 ✅ |
| Factor alpha t-stat | 0.60 ❌ | 0.58 ❌ | 0.61 ❌ |
| Multi-regime | 1 ❌ | 1 ❌ | 1 ❌ |
| **Total** | **4/7** | **5/7** | **4/7** |

**SL+reentry — единственный вариант, проходящий Deflated Sharpe Ratio (p=0.016).**

### Walk-forward H2 (OOS)

| Variant | N | WR | Sharpe | DSR p-value |
|---|---|---|---|---|
| SL=2% (old) | 61 | 54% | 1.30 | 0.052 ❌ |
| **SL=0.75%+reentry** | 61 | 46% | 1.29 | **0.048** ✅ |

### Слабые места (structural, одинаковые для всех)
- Factor alpha t-stat ~0.6 — мало данных для значимости (alpha ~1%/yr)
- Multi-regime: работает только в low_vol (weekend effect = low-vol phenomenon)

## Данные

- 127 trades, 5.3 лет (2021-01 — 2026-05)
- BTC 1h candles: 48000 (2020-11 — 2026-05)
- Macro: KWEB, EWJ, XLK (3-of-3 proxy для prod 5-of-5)
- SL detection: intra-candle low/high для точности

## Deployed

- Commit: `0416d65` (2026-05-23)
- Config: SL=0.75%, bounce=0.5%, re_sl=1.0%, max_h=16
- Cron: check-sl каждый час (Sat+Sun) для bounce detection
- Файлы: `config.py`, `runner.py`, `state.py`, `weekend_signal.py`
