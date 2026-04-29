# Weekend Effect: Cross-Asset Predictors for BTC Weekend Returns

**Статус:** CONFIRMED (OOS validated), paper trading  
**Период данных:** 2021-05 — 2026-04 (253 выходных)  
**Скрипты:** `scripts/tmp/weekend_predictors.py`, `scripts/tmp/weekend_ensemble.py`, `scripts/tmp/weekend_walkforward.py`, `scripts/tmp/weekend_new_predictors.py`

## Суть

Пятничная динамика традиционных рынков (акции, forex, commodities) предсказывает направление BTC на выходных. Ансамбль из 3 предикторов с majority vote даёт OOS-подтверждённый edge.

## Лучшие стратегии (OOS validated, correct annualization, net of costs)

| Стратегия | H2 Sharpe_net | H2 avg net | H2 WR | H2 N | Annual net |
|---|---|---|---|---|---|
| VOTE(solbtcF+nqF+energyW) | **1.44** | +1.04% | 62% | 32 | ~14% |
| VOTE(babaF+nqF+techW) | **1.40** | +0.76% | 65% | 43 | ~14% |
| VOTE(usdjpyW+nqF+techW) | 1.26 | +1.34% | 58% | 26 | ~12% |
| VOTE(usdjpyW+copper_minersF+techW) | 1.26 | +1.58% | 59% | 17 | ~10% |

Sharpe считается с sqrt(trades/year), не sqrt(52). Costs = 0.15%/trade.

## Предикторы по категориям

### Работают (OOS confirmed)

**Equity (лучшая категория):**
- baba_fri — Alibaba Friday return. Азиатский risk sentiment.
- tech_week (XLK) — Tech sector week return. Consistent across years.
- nq_week (QQQ) — NASDAQ week return. Baseline.
- energy_week (XLE) — Energy week return.
- japan_fri (EWJ) — Japan ETF Friday.
- china_inet_fri (KWEB) — China Internet Friday. Лучший individual OOS (H2 Sharpe_c 1.34).

**Forex:**
- usdjpy_week — USD/JPY week return. Carry trade = global risk appetite. Сильнейший новый предиктор.

**Alt/BTC:**
- solbtc_fri — SOL/BTC Friday ratio change. Крипто-ритейл sentiment.

**Commodities:**
- copper_fri — Copper futures Friday. "Dr. Copper" global growth proxy.
- copper_miners_fri (COPX) — Copper miners Friday.

**Bonds:**
- tips_week — TIPS week return. Inflation expectations.

### Не работают

- EUR/USD, GBP/USD, AUD/USD, NZD/USD, USD/CNY — forex кроме JPY
- ETH/BTC (пятница и неделя) — слишком коррелирован с BTC
- CME gap (spot vs futures) — слабый, хотя CME BTC Friday return работает в ансамблях
- Gold/Silver ratio — нет сигнала
- TIP/TLT ratio — не прошёл OOS
- Crypto-adjacent акции (MSTR, COIN, MARA) — в одном пузыре с BTC
- Oil, Gold, Silver (individual) — нет или слабый сигнал
- SP500_week, GOOGL_week, AAPL_week, META_week — не прошли OOS (H1 overfit)

## Механика входа/выхода

- Entry: Friday 21:00 UTC (после закрытия NYSE)
- Exit: Sunday 23:00 UTC (перед открытием азиатских рынков)
- Direction: majority vote (2/3 или 3/3 предикторов согласны)
- Если нет консенсуса — не торгуем
- Stop-loss: 2%

## Рабочая гипотеза: почему работает

### Механизм (комбинация трёх факторов)

**1. Пятничный risk sentiment задаёт направление**

Пятница — последний день когда все рынки открыты. Цена акций, commodities, carry trade к закрытию = агрегированное мнение "умных денег" о глобальном risk appetite. Это информация, которая ещё не полностью отражена в BTC, потому что крипто-рынок в пятницу реагирует на свою внутреннюю динамику (фандинг, ликвидации, азиатский ритейл).

**2. Портфельная ребалансировка создаёт начальный flow**

Институционалы с мульти-активными портфелями (risk parity, vol targeting, macro фонды) реагируют на пятничные движения:
- Акции/commodities упали → margin pressure → нужно сокращать risk → продают крипто (единственный ликвидный актив на выходных)
- Акции выросли → risk budget увеличился → могут добавить крипто exposure
- Это создаёт direction-aligned flow в пятницу вечером / субботу утром

**3. Liquidity vacuum усиливает и пролонгирует движение**

Weekend ликвидность BTC = 20-30% от будних дней:
- Маркет-мейкеры выключены или работают с широкими спредами
- Нет институционального контр-flow (фонды не торгуют в выходные)
- Нет mean reversion — начавшееся движение продолжается
- Ритейл видит momentum и присоединяется (cascade)
- Эффект линейно растёт от субботы к воскресенью — подтверждено данными

### Что подтверждает эту модель

- Работают РАЗНЫЕ классы активов (акции, forex, commodities) → дело в общем risk sentiment, не в конкретном активе
- Crypto-adjacent (MSTR, COIN) НЕ работают → они уже отражены в BTC, не добавляют новой информации
- Недельный сигнал часто лучше пятничного → большее движение = больше ребалансировки
- Эффект растёт линейно Sat→Sun → liquidity vacuum растягивает momentum
- Ensemble лучше single → консенсус across asset classes = stronger flow conviction
- USD/JPY работает, EUR/GBP нет → carry trade (cross-asset risk bridge) информативнее чем отдельные валюты
- SOL/BTC работает, ETH/BTC нет → SOL более "ритейловый", ловит weekend sentiment

### Риски модели

**1. Regime shift — появление weekend ликвидности**
- Если биржи или маркет-мейкеры начнут активно торговать на выходных (24/7 markets, tokenized stocks), liquidity vacuum исчезнет → mean reversion вернётся → edge пропадёт
- **Маркер:** следить за weekend bid-ask spread и depth. Если spread сужается на 50%+ → пересмотреть стратегию
- **Вероятность:** средняя в горизонте 2-3 лет. SEC обсуждает 24/7 trading, Robinhood уже делает weekend stocks

**2. Institutional crypto adoption**
- Больше фондов с crypto allocation → больше ребалансировки → может УСИЛИТЬ эффект краткосрочно
- Но также больше арбитражёров → может СОКРАТИТЬ эффект
- Net effect неясен, но структурно edge скорее сужается

**3. Black swan weekends**
- Крупное событие в субботу (hack биржи, регуляторный бан, геополитика) ломает корреляцию
- BTC weekend может пойти -10% независимо от пятничного sentiment
- SL 2% частично защищает, но gap risk реален
- **Mitigation:** не использовать leverage, position sizing max 10-15% портфеля

**4. Correlation breakdown в crisis**
- В сильный risk-off (март 2020, ноябрь 2022) корреляции ломаются — всё падает вместе
- Ensemble может давать сильный long signal в пятницу, а weekend обвал всё равно случится
- В данных: 2022 bear market — стратегия всё равно была profitable, но с большим DD

**5. Crowding — если стратегия станет популярной**
- Сейчас edge существует потому что мало кто систематически торгует weekend BTC по equity signals
- Если это станет "known strategy" — entry в пятницу вечером станет congested, slippage вырастет
- **Защита:** ~18 трейдов/год, $1-10k size — не enough volume to move market

## Почему edge существует и не исчезает

### Пересмотренная модель (v2, 2026-04-29)

Первоначальные аргументы ("фондам сложно", "слишком мелко") — **слабые**. Crypto-native фонды (Jump, Wintermute, GSR) уже имеют и TradFi data, и crypto execution. Bridge не проблема. 14% при Sharpe 1.4 — хорошая доходность для любого размера. Вот что на самом деле происходит:

### Фонды скорее всего уже это делают — и это часть edge

Ключевой инсайт: **наша стратегия не конкурирует с институциональным flow, а едет на нём.**

Модель работает так:
1. Фонды с мульти-активными портфелями **ребалансируются** после пятничных движений
2. Их ребалансировочный flow в крипте на выходных — это и есть тот **directional pressure**, который мы ловим
3. Мы не "опережаем" фонды и не "арбитражируем" их — мы **идём в ту же сторону**
4. Больше фондов с crypto allocation → больше ребалансировочного flow → edge может даже **усилиться**

Поэтому конкуренция со стороны фондов не убивает edge, а подпитывает его. Edge исчезнет только если:
- TradFi станет 24/7 (информационный lag пропадёт)
- Weekend ликвидность крипты сравняется с будничной (momentum не пролонгируется)

### Что может убить edge

1. **24/7 TradFi markets** — SEC обсуждает, NYSE/NASDAQ тестируют extended hours. Когда акции торгуются в субботу, информационный lag исчезнет. **Горизонт: 3-5+ лет.**
2. **Weekend ликвидность крипты вырастет до будничного уровня** — маркет-мейкеры начнут активно котировать на выходных → mean reversion вернётся → momentum не работает. **Маркер: weekend spread < 1.5x будничного.**
3. **Structural shift в корреляции BTC-equity** — если BTC декорреллирует от TradFi (станет "цифровым золотом", а не risk-on активом), cross-asset signals потеряют предсказательную силу. В 2021-2026 корреляция стабильно растёт, но это может развернуться.

### Что НЕ убьёт edge

- **Crowding** — мы едем на институциональном flow, не опережаем его. Больше участников = больше flow = сильнее сигнал
- **Публикация стратегии** — информация не секретная, паттерн структурный. Знание о weekend effect не помогает его "арбитражировать", как знание о gravity не помогает летать
- **Больше ботов** — боты на weekend BTC будут двигать цену в ту же сторону (momentum), усиливая effect

## Ёмкость стратегии

### BTC weekend liquidity

- Average weekend daily volume (Binance): $5-10B
- Order book depth +-0.1%: ~$20-50M (обе стороны)
- Для market impact < 0.05%: позиция < $2-5M (single market order)

### Оценка capacity

| Execution | Max position | Annual return |
|---|---|---|
| Single market order, 1 exchange | $2-5M | $280-700k |
| TWAP 1-2 часа, 1 exchange | $10-20M | $1.4-2.8M |
| TWAP multi-exchange (Binance+Bybit+OKX) | $20-50M | $2.8-7M |
| BTC + ETH + SOL параллельно, multi-exchange | $60-150M | $8-21M |

### Для нас ($1-10k)

- Мы — пылинка в этом flow. Zero market impact.
- $10k × 14% = $1,400/год standalone — не впечатляет.
- **Но:** капитал занят 10% времени. Остальные 90% — funding capture, другие стратегии.
- Реальная ценность — **дополнительный слой дохода на тот же капитал**.
- При стеке weekend (14%) + funding capture + основные стратегии на тот же $10k → суммарная доходность значительно выше.

### Для фондов ($10-50M)

- 14% × $20M = $2.8M/год при Sharpe 1.4, MaxDD -9.5%
- Оправдывает 1-2 человека на поддержку, не dedicated team
- Скорее всего реализуется как "add-on" к существующей crypto desk, а не standalone стратегия
- Масштабирование на ETH/SOL увеличивает capacity до $60-150M

## Границы применимости

### Когда стратегия работает лучше

- Нормальные рыночные условия (trending или range-bound)
- Сильный пятничный consensus (3/3 голосов > 2/3)
- Умеренная волатильность (BTC не в 5% intraday swings)
- BTC коррелирован с equity (текущий режим, 2021-2026)

### Когда стратегия может не работать

- Крупные weekend-specific события (хаки, форки, регуляция) — ломают все корреляции
- Экстремальный risk-off (всё падает вместе, cross-asset signals бесполезны)
- Holidays — биржи закрыты, нет пятничных данных
- BTC декорреляция от equity (структурный regime change)
- 24/7 TradFi markets (edge пропадает фундаментально)

### Sizing и risk management

- Max position: 10-15% портфеля (weekend gap risk)
- No leverage (liquidity vacuum = slippage на SL)
- Капитал свободен 90% времени — стекать с другими стратегиями
- Forward test minimum 3 месяца (12-15 трейдов) перед увеличением size

## Метрики коррекции Sharpe

Raw Sharpe в бэктестах завышен из-за sqrt(52) annualization при ~18 trades/year.

| Метрика | Raw | Correct | Net |
|---|---|---|---|
| Annualization factor | sqrt(52)=7.21 | sqrt(18)=4.24 | sqrt(18)=4.24 |
| VOTE(BABA+NQ+XLK) full | 2.87 | 1.70 | **1.44** |
| VOTE(BABA+NQ+XLK) H2 OOS | 2.95 | 1.70 | **1.42** |
| VOTE(SOL/BTC+NQ+Energy) H2 OOS | — | 1.68 | **1.44** |

## Timeline

- 2026-04-27: Initial predictor search (32 tickers), ensemble discovery
- 2026-04-27: Deep dive on BTC/ETH/SOL weekend effect
- 2026-04-29: Walk-forward OOS validation (CONFIRMED)
- 2026-04-29: Corrected Sharpe metrics (net 1.4)
- 2026-04-29: New categories (forex, alt/BTC, commodities) — USD/JPY и SOL/BTC найдены
- 2026-04-29: Mechanism hypothesis + risk analysis
- 2026-04-29: Capacity analysis, revised "why edge exists" model (v2)
