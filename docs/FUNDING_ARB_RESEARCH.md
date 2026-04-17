# Funding rate arbitrage — research

**Дата начала:** 2026-04-17
**Статус:** Активное
**Приоритет:** Main track (после пивота с cross-exchange по итогам Gate 2)

## Цель

Оценить прибыльность и операционную сложность **spot-perp hedge arb** (он же "funding rate arbitrage" / "cash-and-carry") на Binance/Bybit/OKX. Ответить:

1. Какой **реалистичный APR** на BTC/ETH/альткоинах?
2. Какой **капитал** требуется для осмысленной доходности?
3. Какие риски доминируют (basis, liquidation, funding sign flip)?
4. Как это **масштабируется** на десятки пар?

## Идея

Не путать с "лонг и плати funding". Арб — это **хедж**:

```
Вход:
  + BTC spot (покупаем)
  + BTC-PERP short (открываем)
  → Net price exposure = 0 (хедж)

Прибыль:
  funding_rate × position_size × holding_time  (если funding > 0)
  минус: commissions on 4 legs + borrow cost (если есть маржа) + basis risk

Выход:
  - BTC spot (продаём)
  - BTC-PERP short close
```

**Когда работает:** funding rate положительный (обычно в бычьем рынке — лонги доминируют, платят шортам).
**Когда не работает:** funding rate отрицательный (медведь, шорты платят лонгам). Закрываемся, ждём.

## Гипотезы

1. **H1:** Средний исторический funding rate на BTC-PERP положительный с APR > 5% на горизонте 12 мес.
2. **H2:** На альткоинах (top-50 по капитализации) бывают периоды funding > 50% APR, длящиеся неделями.
3. **H3:** Basis risk (spot vs perp price divergence) обычно <0.3% и редко >1%.
4. **H4:** Экономика окупается при капитале от $5-10k; масштабируется линейно до ~$100k без значимого slippage.

## Scope первого захода

**Биржи:** Binance, Bybit, OKX — все имеют spot и USDT-margined perpetuals, одинаковая модель funding (каждые 8 часов).

**Пары для исторического анализа:**
- **Core**: BTC-USDT, ETH-USDT
- **High-APR candidates**: SOL, BNB, DOGE, ARB, OP, SUI (топ-30 по объёму)
- **Tail risk**: что-то заведомо волатильное типа PEPE, FLOKI — для сравнения

**Период:** последние 12 месяцев (2025-04 — 2026-03).

## Методология

### 1. Скачать исторические funding rates

Бесплатные REST endpoints **без API ключа**:
- **Binance USDT-M**: `/fapi/v1/fundingRate?symbol=BTCUSDT&limit=1000` — одна запись каждые 8ч
- **Bybit**: `/v5/market/funding/history?category=linear&symbol=BTCUSDT&limit=200`
- **OKX**: `/api/v5/public/funding-rate-history?instId=BTC-USDT-SWAP&limit=100`

Пагинация по времени (before/after). Для 12 мес × 3 биржи × 20 пар — несколько тысяч запросов, но с rate-limit 10-20 req/s легко укладывается.

### 2. Скачать spot цены (reference)

Для basis-анализа: нужна spot цена в те же моменты что funding settlements. Достаточно **klines 1h** с data.binance.vision (проверено на Этапе 1 — доступны все периоды).

### 3. Расчёт APR на каждой бирже-паре

```
annual_apr = mean(funding_rate) × (365 × 3) × 100%
# 3 funding events в сутки, 365 дней

effective_apr = annual_apr × capital_efficiency - transaction_cost_apr
# capital_efficiency = 1 / (spot_amount + perp_margin) — обычно 0.5-0.8 при leverage 2-3x на перпе
# transaction_cost_apr = комиссии size-adjusted
```

### 4. Backtesting базовой стратегии

- **Entry rule:** funding_rate > X% (threshold), например 5% APR
- **Exit rule:** funding_rate < Y% (lower threshold), например 2% APR
- **Position sizing:** фиксированный usd-notional на каждую открытую позицию
- **Portfolio:** до N одновременных позиций (симулирует разнос по парам)

Метрики:
- CAGR (годовая доходность)
- Sharpe (risk-adjusted)
- Max drawdown
- Доля времени в позиции
- Общие комиссии как % от прибыли

### 5. Риск-анализ

- **Funding sign flip** — как часто меняется знак, сколько длится negative period
- **Basis peak** — экстремумы в кризис (COVID март 2020, LUNA май 2022, FTX ноябрь 2022)
- **Liquidation risk** при leverage 2x/3x/5x — по какому % движению выбивает
- **Stablecoin risk** — USDT depeg события в истории

## Операционные вопросы

### Cross-exchange или single-exchange?

- **Single-exchange** (spot и perp на одной бирже): проще, один аккаунт, один API-ключ, margin счёт общий, atomic cancel. **Недостаток**: меньше возможностей — если OKX даёт лучший funding на альткоине, не сможем использовать.
- **Cross-exchange** (spot на Binance, perp на Bybit): можно выбирать лучшие условия. **Недостаток**: два аккаунта, два баланса, transfer risks, complexity.

**Начнём с single-exchange** (Binance) для MVP, потом расширим.

### Требуемая инфраструктура

| Компонент | Для funding arb | Уже есть? |
|---|---|---|
| CCXT адаптер | ✅ | Да (Phase 1) |
| ExchangeManager | ✅ | Да (Phase 1) |
| Исторические данные (klines + funding) | ✅ | Частично |
| Backtester | ✅ | Нет (Phase 3 TODO) |
| Risk-менеджер (margin, liquidation) | ✅ | Нет (Phase 4 TODO) |
| OMS (spot + perp orders) | ✅ | Нет (Phase 4 TODO) |
| Portfolio-мониторинг | ✅ | Нет (Phase 4 TODO) |
| WebSocket real-time | — (достаточно REST/мин) | Нет, не нужен для этого трека |

**Хорошая новость:** funding arb не требует WebSocket для принятия решений. Sampling funding каждую минуту более чем достаточно. Можно стартовать с REST-only.

## План работы

### Этап 1 — Research (сейчас, $0)
- [ ] Даунлоадер исторических funding rates (Binance + Bybit + OKX, 12 мес, 20 пар)
- [ ] Даунлоадер spot klines 1h (для basis)
- [ ] Анализ: APR распределения по парам, волатильность funding, sign flips
- [ ] Простой backtest базовой стратегии с threshold entry/exit
- [ ] Отчёт: `data/reports/funding_research_{period}.md`
- [ ] **Gate:** решение — идём в paper trading или пивот обратно

### Этап 2 — Paper trading MVP (после Gate)
- [ ] Position sizing + risk limits
- [ ] OMS для spot + perp (single-exchange Binance)
- [ ] Executor: entry/exit логика
- [ ] Symulator или testnet запуск на 2-4 недели
- [ ] Метрики vs backtest predictions

### Этап 3 — Live trading MVP (после paper validation)
- [ ] API ключи на реальный Binance (sandbox → limited live)
- [ ] Conservative capital ($1-5k) для обкатки
- [ ] Мониторинг + Telegram алерты
- [ ] Scaling decision после 4-8 недель

## Open questions

1. **Маржа на перпе:** cross-margin vs isolated-margin? Cross эффективнее, но liquidation-риск распространяется.
2. **Плечо:** 1x/2x/3x? Higher = больше ROI, но драстически растёт liquidation risk.
3. **Когда закрываться:** funding_rate < threshold или max_holding_time или basis > X%?
4. **Как хеджить USDT risk?** USDC-based pairs есть, но ликвидность ниже.
5. **Налоги / отчётность** — держать таблицу holding periods по каждой позиции.

## Результаты Этапа 1

**Дата:** 2026-04-17
**Статус:** Завершён

### Данные
- 58 pair-exchange комбинаций (20 пар × 3 биржи)
- Binance/Bybit: 12 месяцев истории, OKX: 3 месяца
- Полные отчёты: `data/reports/funding_analysis.md` и `data/reports/funding_backtest_v2.md`

### Ключевые результаты

**1. Simple threshold strategy: ALL варианты отрицательный PnL**
- Комиссии (20-24 bps roundtrip) превышают сигнал на всех threshold-ах
- Ни одна конфигурация не вышла в плюс

**2. Cross-exchange funding arb: 0/114 пар прибыльных**
- Даже при 8 bps perp-only roundtrip — ни одна пара не прибыльна

**3. Buy-and-hold работает: 35/58 прибыльных**
- Топ пары: LINK/SUI/LTC/BTC — 3.5-5% APR
- Max drawdown < 0.5%
- Но: 40% пар имеют persistent negative funding (APT -17%, ATOM -13%)

### Gate funding-1

| Трек | Решение | Обоснование |
|------|---------|-------------|
| Active trading (threshold/cross-exchange) | **RED** | Все варианты отрицательный PnL после комиссий |
| Passive yield (buy-and-hold select pairs) | **GREEN** | 3.5-5% APR на BTC/ETH/LINK/SUI, DD < 0.5% |

### Вывод

Funding arb — это **yield product** (~4% APR), а не trading strategy. Эквивалент "процентного счёта в крипте" с counterparty risk биржи. Не main trading track. Пивот на on-chain CEX-DEX research.

---

## Источники (в процессе исследования будут обновляться)

- Binance Futures API docs: https://developers.binance.com/docs/derivatives/usds-margined-futures
- Bybit API docs: https://bybit-exchange.github.io/docs/v5/market/history-fund-rate
- OKX API docs: https://www.okx.com/docs-v5/en/#public-data-rest-api-get-funding-rate-history
- Classic research: https://arxiv.org/abs/2209.13793 (crypto funding arb academic)
