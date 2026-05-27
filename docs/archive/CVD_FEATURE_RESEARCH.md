# CVD (Cumulative Volume Delta) as Feature — DEAD END

**Дата:** 2026-05-28
**Статус:** Dead end (buy/sell classification бесполезна на всех таймфреймах)

## Контекст

После серии исследований tick bars / imbalance bars (TIB) мы установили, что tick-level buy/sell% = шум на крипто фьючерсах (MFE/MAE=0.99, sell%≈50% даже при крупных движениях).

**Новая гипотеза:** CVD на более длинном таймфрейме (4H) может уловить institutional accumulation/distribution, которое не видно на тиках. Sustained CVD trend при flat price = divergence → directional signal.

## Что тестировали

**Данные:** Binance Futures 4H klines с полем `taker_buy_base_volume` (12 мес, 8 монет: BTC, ETH, SOL, DOGE, SUI, LINK, AVAX, ADA). 2160 свечей на монету.

### 11 CVD-фичей

1. **cvd_slope_{6,12,24}** — наклон CVD (линейная регрессия), нормализованный на avg volume
2. **cvd_price_diverg_{6,12,24}** — расхождение CVD и price return (accumulation/distribution)
3. **buy_ratio_{6,12,24}** — taker_buy / total volume за окно (отклонение от 0.5)
4. **vol_delta_zscore_{24,48}** — z-score volume delta

### Сигналы

Simplified Miro-like: S/R levels (60 candle lookback, 2+ touches) → breakout entry → hold 6 candles (24h).

**461 сигнал** по 8 монетам, baseline WR = 50.1%.

## Результаты

### Корреляции с PnL

| Feature | corr(PnL) | corr(win) |
|---|---|---|
| cvd_slope_6 | +0.020 | -0.001 |
| cvd_slope_12 | +0.032 | +0.002 |
| cvd_slope_24 | +0.025 | +0.047 |
| cvd_price_diverg_6 | -0.048 | +0.027 |
| cvd_price_diverg_12 | **-0.051** | +0.015 |
| cvd_price_diverg_24 | -0.006 | +0.039 |
| buy_ratio_6 | +0.019 | -0.017 |
| buy_ratio_12 | +0.017 | +0.005 |
| buy_ratio_24 | +0.007 | +0.012 |
| vol_delta_zscore_24 | +0.019 | -0.024 |
| vol_delta_zscore_48 | +0.004 | -0.045 |

**Все |corr| < 0.05** — статистически ноль.

### CVD Divergence как фильтр (работает НАОБОРОТ)

**Лонги + cvd_price_diverg_12:**
- CVD "accumulation" (aligned): N=120, WR=52.5%, avg PnL=**-0.11%**
- CVD "no accumulation": N=120, WR=48.3%, avg PnL=**+0.20%**

**Шорты + cvd_price_diverg_12:**
- CVD "distribution" (aligned): N=110, WR=52.7%, avg PnL=+0.11%
- CVD "no distribution": N=111, WR=46.8%, avg PnL=-0.64%

CVD divergence не помогает, а в ряде случаев вредит.

### ML: GradientBoosting (CVD-only)

- **Accuracy: 47.5% ± 3.3%** (5-fold CV)
- **Baseline: 50.1%**
- Модель хуже random → CVD фичи = чистый шум

### buy_ratio на 4H

Mean buy_ratio = 49.3-49.4% для winners и losers — идеальные 50/50. Taker buy/sell classification не несёт информации даже при часовой агрегации.

## Почему не работает (общий вывод по блоку)

Тестировали buy/sell classification в крипте 4 способами:

| Метод | ТФ | Результат |
|---|---|---|
| **TIB** (tick imbalance bars) | тиковый | MFE/MAE=0.99, random |
| **Informed flow** (absorption, stealth) | тиковый | MFE/MAE=0.99, random |
| **Tick sell%** для direction | тиковый | не улучшает RSI |
| **CVD** (4H aggregation) | 4H | |corr|<0.05, ML<random |

**Причина структурная:**
1. **Wash trading** — inflates volume без directional bias
2. **Delta-neutral MM** — маркет-мейкеры хеджируют немедленно, buy≈sell по объёму
3. **Нет informed flow** — в крипте нет инсайдеров как на акциях
4. **Liquidations ≠ informed** — крупнейший crypto-specific flow (ликвидации) вынужденный, не информированный
5. **OTC невидим** — institutional accumulation идёт через OTC desks (60-70% крупных сделок), на бирже не отражается

**Блок buy/sell classification для крипто фьючерсов — ЗАКРЫТ.**
TIB, VIB, DIB, CVD на любом таймфрейме — бесполезны для directional alpha.

## Подтверждение из литературы

Наши выводы совпадают с академическими исследованиями 2024-2026:

### Easley & O'Hara et al. — VPIN предсказывает regime, не direction
- **Paper:** [Microstructure and Market Dynamics in Crypto Markets](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4814346) (SSRN 2024, опубликовано 2025)
- Авторы PIN/VPIN (Easley, O'Hara) применили микроструктурные метрики к 5 крипто
- VPIN имеет предсказательную силу для **волатильности и ликвидности** (полезно для MM и хеджирования)
- Но это **не directional prediction** — это prediction of regime (высокая/низкая токсичность)
- **Наш вывод совпадает:** buy/sell split полезен для regime detection, не для direction

### Anastasopoulos et al. — order flow работает для cross-section
- **Paper:** [Order Flow and Cryptocurrency Returns](https://www.sciencedirect.com/science/article/pii/S1386418126000029) (2024)
- ML на order flow фичах бьёт фундаментальные модели (Sharpe 3.61 vs 2.57)
- **Но:** cross-section (какая монета обгонит другие), не direction prediction одной монеты
- Используют нелинейные ML на десятках фичей из нескольких бирж — не наш масштаб
- **Наш вывод совпадает:** мы тестировали per-coin direction → шум

### VPIN Alpha в BTC — умирает к 2026
- **Статья:** [I Used a 2012 Market Microstructure Paper to Find Alpha in BTC. It Worked — But It's Dying](https://medium.com/coinmonks/i-used-a-2012-market-microstructure-paper-to-find-alpha-in-btc-it-worked-but-its-dying-500f9bc0fc94) (Medium/Coinmonks, апрель 2026)
- VPIN давал alpha на BTC futures, но **к 2026 signal уже не significant**, gross returns halving каждый год
- **Не работает на ETH и SOL** — BTC-specific red flag
- Работает только в bull months, в bear = ноль
- **Наш вывод совпадает:** даже то что когда-то работало — арбитражировано

### Дополнительно
- [Explainable Patterns in Cryptocurrency Microstructure](https://arxiv.org/abs/2602.00776) (arxiv 2026) — LOB data в крипте highly noisy, extracting signal = hard
- [Bitcoin wild moves: Evidence from order flow toxicity and price jumps](https://ideas.repec.org/a/eee/riibaf/v81y2026ics0275531925004192.html) (2026) — VPIN предсказывает jumps (волатильность), не direction

### Сводка: наши выводы vs литература

| Наш вывод | Литература | Совпадение |
|---|---|---|
| sell%/buy% ≈ 50%, шум для direction | VPIN → volatility/regime, не direction | Да |
| CVD не предсказывает PnL per-coin | Order flow → cross-section, не individual coin | Да |
| Нет informed flow как на акциях | VPIN alpha на BTC умирает к 2026 | Да |
| Tick bars бесполезны для direction | VPIN полезен для MM risk, не для alpha | Да |

## Что осталось полезного

1. Binance Futures klines downloader с taker_buy_volume (кэширование в parquet)
2. Подтверждение: volume_ratio и volume_trend (total volume, без buy/sell split) — всё ещё полезные фичи в Miro ML (top-3 by importance)
3. Урок: **total volume = полезен, buy/sell split = шум для direction**
4. Единственный потенциал buy/sell split: regime/volatility detection (VPIN-style) — но это другая задача

## Скрипты

- `scripts/research/cvd_feature_test.py` — полный тест CVD фичей
- Предшествующие: `scripts/research/cascade_tick_backtest.py`, `informed_flow_detector.py`, `bigmove_tick_filter.py`
