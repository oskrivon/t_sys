# Weekend SL Recovery: Post-SL Action Analysis

**Дата:** 2026-05-23
**Статус:** DEAD END

## Гипотеза

При сильном консенсусе предикторов (4/5 или 5/5 UP) и срабатывании SL (2%), BTC должен восстанавливаться в направлении предикторов. Значит:
- Flip в SHORT после SL — вредит
- Re-entry LONG после стабилизации — должен работать

## Данные

- BTC 1h свечи: 2020-11-30 — 2026-05-23 (48000 candles)
- Macro predictors: KWEB(fri), EWJ(fri), XLK(week) — прокси 3-of-3 для prod 5-of-5
- 43 tradeable weekends, 10 SL hits (23.3%)
- SL timing: 2 fast (10-11h), 2 medium (22-23h), 6 slow (43-50h)

## Результаты

### Recovery rate после SL hit

| Консенсус | N | Recovery WR | Avg recovery | Median |
|---|---|---|---|---|
| >= 2 votes | 10 | 40% | -0.20% | -0.22% |
| >= 3 votes (strong) | 7 | 57% | -0.07% | +0.06% |

Recovery rate даже при сильном консенсусе — coin flip. Avg recovery отрицательный.

### Стратегии после SL hit (consensus >= 3, N=7)

| Стратегия | Avg PnL | WR | Total |
|---|---|---|---|
| A) Accept SL loss | -2.00% | 0% | -14.00% |
| B) Flip to SHORT | -1.93% | 0% | -13.48% |
| C) Re-enter LONG +0h | -2.43% | 0% | -16.99% |
| C) Re-enter LONG +8h | -2.15% | 14% | -15.03% |

WR = 0% для всех кроме +8h cooldown (14% = 1 из 7).

### Влияние на полную стратегию (consensus >= 3, 37 trades)

| Вариант | Sharpe | Total PnL |
|---|---|---|
| Baseline (просто SL) | 2.36 | +28.27% |
| + flip в SHORT | 2.38 | +28.79% |
| + re-enter LONG +0h | 1.98 | +25.28% |
| + re-enter LONG +8h | 2.18 | +27.24% |

Re-entry LONG ухудшает Sharpe с 2.36 до 1.98. Flip чуть лучше baseline (+0.02 Sharpe) — в пределах шума.

### Speed of drop analysis

- **FAST (<=12h):** 2 trades, recovery 50%, avg -0.35%. Слишком мало данных.
- **MEDIUM (13-24h):** 2 trades, recovery 50%, avg +0.09%.
- **SLOW (25h+):** 6 trades, recovery 33%, avg -0.24%. Остается мало времени до exit.

Текущий кейс (2026-05-22): SL@10h, BTC восстановился +0.42% — но это 1 из 2 fast SL.

## Вывод

**DEAD END.** Гипотеза не подтверждается:
1. После SL hit BTC НЕ восстанавливается в направлении предикторов (WR 40-57%, avg отрицательный)
2. Re-entry в ту же сторону ухудшает результат (-0.38 Sharpe)
3. Flip в SHORT = noise (±0.02 Sharpe)
4. Оптимальная стратегия — принять SL loss и не действовать

Предикторы прогнозируют 50h интервал, а не мгновенное направление. SL hit = структурный слом прогноза, не временная коррекция.
