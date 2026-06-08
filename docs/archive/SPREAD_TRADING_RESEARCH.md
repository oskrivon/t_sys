# Spread Trading в крипте: полный ресёрч

**Дата:** 2026-06-08
**Статус:** DEAD END (все 4 типа)
**Мотивация:** Marcos López de Prado, торговля спредом фьючерсов. Проверка применимости к крипте.

## Протестированные типы

### 1. Calendar Spread (Quarterly vs Perpetual) — DEAD END

**Гипотеза:** Basis (Q - Perp) стационарен, mean-revertится. Де Прадо: fractional diff, structural breaks, OU process.

**Данные:** BTC, ETH, Binance USDT-M, 6 месяцев, 1h (3947 obs на контракт).

**Результаты ADF (стационарность):**
- BTC Q2: p=0.33, non-stationary. Half-life 372h
- BTC Q3: p=0.32, non-stationary. Half-life 58h
- ETH Q2: p=0.44, non-stationary. Half-life 225h
- ETH Q3: p=0.32, non-stationary. Half-life 41h

**Power law fit:** `basis = a * dte^b`, R2=0.97 (BTC), R2=0.96 (ETH). Convergence curve хорошо описывает basis.

**Z-score backtest (кажется работает):** ETH Q2: 92 trades, WR 100%, Sharpe 12.4. Но edge ~0.05% per trade.

**Convergence backtest (с реальными costs):**

| DTE | Basis | Funding cost | Fees | NET |
|-----|-------|-------------|------|-----|
| 90d | +0.55% | -0.97% | -0.08% | **-0.50%** |
| 60d | +0.15% | -0.65% | -0.08% | **-0.57%** |
| 30d | +0.22% | -0.32% | -0.08% | **-0.18%** |
| 14d | +0.18% | -0.15% | -0.08% | **-0.05%** |

**Причины смерти:**
1. Funding на perp-ноге (~3.9% APR) = basis income (~3-5% APR). Нет edge.
2. Ликвидность quarterly = 0.1% от perps (6-131 vs 7000-195000 контрактов/час).
3. Residuals ~0.13% при costs ~0.12%. Нет margin of safety.

**Почему в TradFi работает, а в крипте нет:**
- TradFi: нет perpetual funding → carry бесплатный
- TradFi: quarterly = основной инструмент → ликвидность есть
- Крипта: perps заменили quarterlies

### 2. Dynamic Basis (Perp vs Spot) — DEAD END

**Гипотеза:** Экстремальные отклонения perp от spot mean-revertируются.

**Данные:** BTC, 5m, 3 месяца (25920 obs).

**Результаты:**
- Basis: mean=-5 bps, std=0.9 bps
- **Basis ни разу не превысил 10 bps за 3 месяца**
- Max: -10 / +6 bps. Round-trip fees: 8-30 bps.
- 494 сделки при 5bps threshold: **0% win rate**

**Причина:** ММ держат спред <1 bps. Это простейший арбитраж, полностью автоматизирован.

### 3. Cross-Exchange Basis — DEAD END (ранее)

Протестировано ранее. ММ выедают межбиржевой спред.

### 4. Cross-Asset Spread (BTC/ETH, SOL/BNB и др.) — DEAD END

**Гипотеза:** Крипто-пары коинтегрированы, ratio mean-revertится (Johansen, Engle-Granger).

**Данные:** BTC, ETH, SOL, BNB perps, 12 месяцев, 1h (8640 obs).

**Коинтеграция:**

| Pair | Coint_p | HL (hours) | Rolling coint% |
|------|---------|------------|----------------|
| BTC/ETH | 0.28 | 1354 | 33% |
| BTC/SOL | 0.39 | 3162 | 22% |
| ETH/SOL | 0.23 | 930 | 11% |
| BTC/BNB | 0.82 | 1485 | 22% |
| ETH/BNB | 0.92 | 3360 | 11% |
| SOL/BNB | 0.80 | 4786 | 22% |

**Ни одна пара не коинтегрирована стабильно.** BTC/ETH — best case — только 33% окон.

**Z-score backtest:**

| Pair | PnL | Sharpe |
|------|-----|--------|
| BTC/ETH | -26% | -0.96 |
| BTC/SOL | +8% | 0.31 |
| ETH/SOL | +11% | 0.41 |
| SOL/BNB | +24% | 0.82 |

BTC/ETH: 34 стопа = -63%, тренд доминирует. SOL/BNB: Sharpe 0.82 без учёта реальных costs, вероятно подгонка.

**Причина:** Крипто-активы имеют общий beta к BTC, но relative value нестабильно. Нет стабильного равновесия для stat arb.

## Общий вывод

**Spread trading (de Prado style) не применим к крипте** из-за фундаментальных отличий инфраструктуры:
1. Perpetual funding mechanism автоматически закрывает basis арбитражи
2. ММ в крипте специализируются именно на spread capture
3. Quarterly futures — нишевый продукт с нулевой ликвидностью
4. Крипто-пары не имеют стабильной коинтеграции

**Что работает из де Прадо для крипты:** математика (fractional diff, structural breaks) применима к directional стратегиям, но НЕ к spread trading.

## Скрипты

- `scripts/research/calendar_spread_research.py` — ADF, OU, z-score backtest
- `scripts/research/calendar_spread_convergence.py` — convergence model, cost analysis
- `scripts/research/dynamic_basis_research.py` — perp-spot basis analysis
- `scripts/research/cross_asset_spread.py` — multi-pair cointegration screening
