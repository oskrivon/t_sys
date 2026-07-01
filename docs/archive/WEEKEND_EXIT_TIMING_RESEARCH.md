# Weekend Exit Timing Optimization — DEAD END

**Дата:** 2026-06-01
**Статус:** CLOSED — текущий выход Sun 23:00 оптимален, альтернативы не прошли robustness check.

## Вопрос

Можно ли выходить из Weekend-сделки оптимальнее, чем фиксированный Sun 23:00 UTC?

## Протестировано

### 1. Почасовой профиль PnL (112 trades, 4h resolution, 5.3 года)

Профит растёт монотонно Fri→Sun, peak Sharpe на h=35 (Sun 08:00) = 2.03, текущий h=51 = 1.89. Но total return на h=51 (+159%) вдвое выше h=35 (+84%). Профит продолжает расти до h=55 (Mon 04:00, +173%).

### 2. Conditional exit: "if Saturday DOWN, exit early"

Sat UP (66% сделок): Sharpe 2.2–2.9, сильный edge.
Sat DOWN (34%): Sharpe ~0, но 45% recover к settlement.

Threshold scan лучший: exit при PnL < -0.25% на Sun 08:00 → Sharpe 2.10 vs 1.89.
**WF OOS: +0.02 Sharpe** — шум, не edge.

### 3. Take-Profit — все уровни хуже baseline

TP 0.3%–3.0% — все снижают total return. Лучший TP 2.5% даёт Sharpe 2.03, total 113% vs baseline 159%. Hold-to-settlement всегда лучше.

### 4. Momentum/Volatility exits

| Метод | Full Sharpe | vs Base |
|---|---|---|
| Momentum reversal (peak>1% then <0%) | 1.94 | -0.5% |
| Trailing stop (peak>1% trail 0.5%) | 2.14 | -32.7% |
| Vol spike + losing | 1.37 | -41.7% |
| 4 adverse candles | 2.03 | +7.0% |

Trailing stops повышают Sharpe, но **катастрофически режут total** — фиксируют profit слишком рано.

### 5. Разный exit для Long vs Short — лучший кандидат

L=h55 (Mon 04:00), S=h51 (Sun 23:00): Sharpe 2.14, total +173%.

**Robustness check (4/7 PASS):**

| Тест | Результат |
|---|---|
| Bootstrap 95% CI | FAIL ([-0.04, +0.57], содержит 0) |
| Bootstrap 90% CI | PASS ([+0.01, +0.52]) |
| Permutation test | PASS (p=0.004) |
| Subperiods 3/3 | PASS |
| LOO 6/6 | PASS |
| Monte Carlo | PASS (p=0.02) |
| Paired t-test | FAIL (p=0.13) |
| Sample size | FAIL (need 234 longs, have 71) |

Cohen's d = 0.18 (small effect), avg improvement +0.25%/trade при std 1.36%.

## Вывод

Эффект L/S exit split реальный (permutation p=0.004), но **слишком маленький для reliable deployment** при текущем sample size. Paired t-test не значим, bootstrap CI содержит 0, sample underpowered (нужно 3x больше данных).

**Решение:** keep current Sun 23:00 exit. Пересмотреть через ~6 месяцев (ещё ~30 long trades).

## Скрипты

- `scripts/research/weekend_exit_timing.py` — hourly PnL profile, take-profit, MFE decay
- `scripts/research/weekend_conditional_exit.py` — Saturday momentum conditional exit
- `scripts/research/weekend_exit_ideas.py` — 3 ideas + WF validation
- `scripts/research/weekend_exit_robustness.py` — bootstrap, permutation, LOO, power analysis
