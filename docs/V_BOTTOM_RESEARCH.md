# V-Bottom Dip Buying Strategy Research

**Дата:** 2026-05-24
**Статус:** PAPER TRADING — имплементация

## Концепция

BTC падает ≥3% за 24h при высокой волатильности (NATR > median) и без macro selloff (NQ T-1 не упал) → покупаем → hold 24h → выход.

## Исследование (6 этапов)

### Этап 1: Базовый паттерн
BTC drop 3%/24h → hold 48h. 438 non-overlapping trades, WR 56.8%, avg +0.19%.
Z-score vs random: **5.67** (p<0.0001). Паттерн реален.

### Этап 2: Фичи
Тестировали RSI, NATR, volume, trend, drop speed на 435 trades.
Значимые: **NATR (p=0.020)**, drop depth (p=0.053).
RSI 20-25 sweet spot (WR 69%), но мало trades.
Green candle at entry: WR 67.6%, avg +2.38% — но N=37.

### Этап 3: TradFi контекст (ключевая находка)
| Контекст | N | WR | Avg PnL |
|---|---|---|---|
| **Crypto-only (NQ>-0.5%)** | 271 | **61.6%** | **+0.77%** |
| NQ тоже упал | 233 | 52.4% | -0.40% |
| **Macro selloff (NQ<-1%)** | 117 | **44.4%** | **-0.96%** |

Structural edge: crypto-only drop = ликвидации/deleveraging, не macro risk.

### Этап 4: SHORT сторона — не работает
SHORT (sell pump + NQ filter): WR 52.9%, Sharpe 0.29, scorecard 3/7.
CMKT beta = -0.016 (t=-5.01) — просто шортит BTC.
**Стратегия = smart BTC long с timing, не mean-reversion.**

### Этап 5: Exit strategy
- **Fixed hold 12-24h = лучший exit.** Sharpe 2.08-2.45.
- Trailing stop **убивает стратегию** (WR 30-38%). BTC чопит, trailing выбивает.
- SL/TP пары marginal.

### Этап 6: Look-ahead bias (критическая находка)

**Timestamp shift test** выявил look-ahead в NQ фильтре:

| NQ shift | Sharpe | CAGR |
|---|---|---|
| 0d (look-ahead) | **1.98** | +64% |
| +1d (future!) | 1.17 | +34% |
| **-1d (honest)** | **1.05** | **+25%** |
| -2d | 0.66 | +10% |
| random | 0.53 | +5% |

Ratio shift0/shift-1 = 1.89 → confirmed look-ahead. Scorecard (CPCV, DSR, PBO) не ловит look-ahead потому что bias consistent across folds.

**Добавлен timestamp shift test в validation framework.**

## Честные числа (без look-ahead)

| Метрика | Значение |
|---|---|
| Trades | ~300, ~55/yr |
| WR | 60.0% |
| Avg PnL/trade | +0.56% |
| **CAGR (no leverage)** | **+25%/yr** (compound, full deposit) |
| Sharpe | 1.05 |
| MaxDD | -24.4% |

Annual breakdown:
```
2021: 89 trades, WR 61.8%, +105.7%  (volatile year)
2022: 51 trades, WR 58.8%, -9.0%    (bear market)
2023: 27 trades, WR 63.0%, +31.9%
2024: 62 trades, WR 61.3%, +23.5%
2025: 48 trades, WR 60.4%, +2.3%
2026: 19 trades, WR 42.1%, -6.6%    (partial year)
```

## Слабые места

1. **BTC long bias** — SHORT не работает, alpha ≈ 0, это smart beta
2. **2022 = -9%** — bear market dips не bouncing'уются
3. **2026 YTD = -6.6%** — текущий год пока отрицательный
4. **MaxDD -24%** — серьёзно для стратегии без leverage
5. **NQ T-1 = грубый фильтр** — вчерашний close, не real-time

## Конфигурация

```python
drop_threshold = 3.0%    # min BTC drop
drop_window = 24         # hours lookback
hold_period = 24         # hours (fixed, no trailing)
natr_period = 14         # NATR computation
natr_threshold = expanding_median  # no look-ahead
nq_filter = "QQQ T-1 daily ret > -0.5%"  # skip macro selloffs
fees = 0.1% round trip
```

## Dead ends в процессе

- Альты: BTC-only, альты не bouncing'уются (WR 44-49%)
- Trailing stop: WR 30-38%, убивает strategy
- NQ intraday: не лучше daily T-1, данных только 2.4 года
- Commodities/bonds: не тестировали (overfit risk при N=300)
