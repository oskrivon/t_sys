# Equity Market Research: LLM Earnings & Micro-Cap Strategies

## Дата: 2026-05-21

## Контекст

Исследование возможностей на фондовом рынке: earnings-based стратегии с LLM, micro-cap pump/dump,
SEC 8-K event-driven. Цель — найти некоррелированный с крипто источник alpha.

---

## 1. PEAD Baseline (Earnings Surprise -> Drift)

**Инструмент:** `src/earnings/` — полный MVP pipeline (config, fetcher, backtest, report, scorer).

**Результаты S&P 100, 2023-01 -- 2025-06, threshold 3%:**

| Hold | Trades | WR | Avg/trade | Sharpe/trade | Sharpe annual | Annual/capital |
|------|--------|-----|-----------|-------------|---------------|----------------|
| 1d | 724 | 48% | -0.098% | -0.036 | -0.63 | -1.9% |
| 3d | 724 | 50% | -0.036% | -0.010 | -0.17 | -0.4% |
| 5d | 724 | 50% | +0.095% | 0.022 | **0.39** | +0.8% |
| 10d | 724 | 54% | +0.624% | 0.114 | **1.97** | +3.5% |

**Quintile monotonicity (5d hold):**
- Q1 (worst surprise): -0.41% avg
- Q4 (best surprise): +0.57% avg
- PEAD effect visible, но per-trade edge мизерный

**Capital-adjusted reality:**
- Max concurrent positions: 36 (earnings season)
- Capital needed: $360K для $10K/position
- Annual return on capital: **0.8%** (не 25% как наивный compute_metrics показывал)

**Вывод:** PEAD существует, но edge слишком мал для profitable trading после costs.
Short сторона не работает в bull market. LLM scoring (Phase 2) отложен — baseline слишком слаб.

---

## 2. Евротранс (EUTR) — кейс "щиткоин-акции" на MOEX

**Хронология:**
- IPO ноя 2023: 250 руб, привлекли 13.5 млрд, 20K розничных инвесторов
- Янв 2024: пик 485 руб (+94%), free float 20%
- Июл 2024: ~108 руб (-57% от IPO)
- Мар 2026: первый техдефолт по облигациям
- Апр 2026: второй техдефолт, рейтинг ruC, ATL 61 руб
- Май 2026: иски на 23+ млрд, долг 97 млрд при EBITDA 24 млрд

**Можно ли было заработать:** да, но шорт невозможен.
EUTR отсутствует в списке ликвидных бумаг Тинькофф для маржинальной торговли.
3-й эшелон MOEX = нет шорта, нет опционов, нет фьючерсов.

**Вывод по MOEX micro-cap:** инфраструктура не позволяет системно эксплуатировать паттерн.
Long-only на pump phase = gambling, не стратегия.

---

## 3. SEC 8-K Dilution Detector (US micro-cap)

**Тезис:** парсить 8-K filings в реальном времени, ловить dilutive offerings, шортить.

**Инфраструктура:**
- SEC EDGAR EFTS API: бесплатно, 10 req/sec, sub-second latency
- ~2,000 dilutive offerings/год, ~1,200 micro-cap
- Академически подтверждён negative abnormal return

**Проблема: execution costs = edge**
- Shares to borrow: доступны в 30-40% случаев
- Locate fee: $0.02-0.50/share (2-50% annualized)
- Bid-ask spread: 1-5% для micro-cap
- Market impact: 2-5% на $50K позицию
- Announcement gap: значительная часть move на overnight gap

**Конкуренция:**
- Two Sigma/Citadel НЕ торгуют micro-cap (capacity слишком мала для $60B AUM)
- Конкуренты: prop traders, мелкие quant shops, retail с locate brokers
- Но это не помогает — costs, а не конкуренция, убивают edge

**Вывод:** edge теоретически 5-8%/trade, costs 3-5%/trade. Net ~0-2%.
Не worth building при текущем capital scale.

---

## 4. Hedge Fund Industry Reality

**Средний хедж-фонд:**
- 2011-2020: 5.0%/год vs S&P 500 14.4%/год (в 3 раза хуже)
- 80% фондов проигрывают S&P 500

**Элита:**
- Medallion (Renaissance): 66% gross / 39% net, закрыт с 1993
- D.E. Shaw: 14-36%/год
- Citadel: 15-25%/год

**Бизнес-модель:** 2% management fee + 20% performance fee.
Фонд с $10B AUM получает $200M/год только за management, даже при 0% return.
Большинство фондов зарабатывают на fees, не на alpha.

**Medallion approach:** 500+ мелких некоррелированных стратегий × $20-100M capacity каждая.
Ключ — количество сигналов, не один гениальный сигнал.

---

## 5. Стратегический вывод

**Фондовый рынок для нашего масштаба ($50-200K) не подходит:**
- PEAD edge ~0.1%/trade, costs ~0.03% → net negligible
- Micro-cap short: costs = edge
- MOEX micro-cap: нет шорта
- Нужна команда PhD и $10B чтобы играть как Medallion

**Правильный путь — портфель некоррелированных крипто-стратегий:**

| Стратегия | Sharpe | Annual | Статус |
|-----------|--------|--------|--------|
| Weekend | 1.4 | 15% | **LIVE** |
| ??? #2 | 0.8+ | 8%+ | Нужен ресёрч |
| ??? #3 | 0.8+ | 8%+ | Нужен ресёрч |
| **Портфель** | **~2.0** | **~20%** | |

Крипто уникален: 24/7, низкие costs, доступный leverage, информационные лаги.

---

## Код

- `src/earnings/` — полный MVP (config, models, db, fetcher, backtest, report, scorer)
- `src/backtest/presets.py` — добавлен `us_equities_spot()` cost model
- `scripts/run_earnings_backtest.py` — CLI runner
