# Cascade Tick Bars Research — DEAD END

**Дата:** 2026-05-25
**Статус:** Dead end (направление после триггера случайное)

## Гипотеза

Tick bars (фиксированное количество сделок = 1 бар) улучшат cascade/liquidation detection по сравнению с time bars (1m свечи):
- Фильтрация шума в тихие часы
- Sell%/buy% давление внутри бара как доп-фича
- Imbalance bars (Lopez de Prado) как альтернатива

## Что тестировали

### Bar types
- **Time bars (1m)** — baseline
- **Tick bars (500 trades/bar)** — фиксированный размер
- **Imbalance bars** — закрываются по кумулятивному buy/sell дисбалансу

### Trigger
`|return| > 0.5%` + `volume > 3x avg` → вход в направлении движения

### Exit strategies (9 вариантов)
- hold_5, hold_10 — фиксированный hold
- trail_0.3%, trail_0.5% — trailing stop
- dur_expand — бар длиннее 2x триггера (активность затухает)
- imb_flip — sell% разворачивается
- mom_fade — 2 бара с падающим |return|
- combined_2 — 2+ exhaustion сигнала
- any_exhaust — первый exhaustion сигнал

### Entry filter
sell% > 80% (только при сильном давлении продавцов)

## Результаты

### 3-дневный тест (ложный позитив)
| Bar type | N | WR | Net | Sharpe |
|---|---|---|---|---|
| Time (1m) | 15 | 13% | -0.464% | -1.22 |
| Tick (500) | 2 | 100% | +0.174% | +1.27 |
| Imbalance | 13 | 31% | -0.157% | -0.37 |

### 90-дневный тест (реальность, 5 монет, Binance Vision aggTrades)
| Strategy | N | WR | Net/trade | Annual | Sharpe |
|---|---|---|---|---|---|
| hold_10 | 144 | 47% | -0.101% | -59% | -3.41 |
| trail_0.5% | 144 | 51% | -0.107% | — | -3.16 |
| mom_fade | 144 | 40% | -0.154% | — | -4.76 |
| combined_2 | 144 | 41% | -0.158% | — | -5.88 |

С sell%>80% фильтром: **хуже** (89 trades, WR=44%, net=-0.174%)

### Per-coin
| Coin | N | WR | Avg net |
|---|---|---|---|
| AVAX | 5 | 40% | +0.248% |
| LINK | 9 | 56% | -0.177% |
| SUI | 22 | 55% | -0.101% |
| SOL | 19 | 26% | -0.220% |
| DOGE | 34 | 29% | -0.256% |

## Почему не работает

**MFE ≈ MAE (+0.570% vs +0.556%)** — после триггера цена движется симметрично в обе стороны. Trigger детектирует волатильность, но не предсказывает направление. Вход на закрытии trigger bar = каскад уже произошёл, continuation ≈ 50/50.

## Что полезного осталось

1. **Стриминговый парсер Binance Vision aggTrades** — `scripts/research/cascade_tick_backtest.py`, обрабатывает 90 дней без OOM
2. **Tick bar builder** — переиспользуем для других стратегий
3. **Sell% метрика** — может быть полезна как фильтр в других стратегиях (big move detector)
4. **Урок: 3-дневный backtest = шум**, всегда проверять на 30+ trades

## Дополнительные эксперименты (2026-05-25)

### Informed Flow Detection
- Absorption (vol>2x, price_impact<0.3x), stealth buy/sell (sell% divergence 3 bars), breakout after quiet
- 2090 signals / 90 дней / 5 монет (SOL, DOGE, SUI, AVAX, LINK)
- **MFE/MAE = 0.99** по всем типам — чистый random
- Strength filter (top 25%) не помогает (MFE/MAE=0.97)
- Вывод: на крипто фьючерсах нет "informed flow" как на акциях

### Big Move + Tick Direction
- Сравнение RSI vs tick sell% для direction prediction на big move events
- Tick momentum WR=33%, tick contrarian WR=67% (N=24), RSI WR=61%
- **Внимание: look-ahead bias в event selection** — абсолютные цифры невалидны
- Относительное сравнение: tick bars не улучшают RSI

### TradFi Tick Shadow
- NQ big days (|ret|>1%) → BTC catch-up post-close через tick bar features
- 44 events / 180 дней. Follow rate **48%** (random)
- Desync filter (BTC не отреагировал на NQ): WR=40% — хуже random
- Post-close tick sell% = 47-53% на **всех** событиях — нет directional flow
- Вывод: крипто не "тень" equity на тиковом уровне

## Общий вывод по tick bars на крипто

**Tick bar sell%/buy% на крипто фьючерсах не несёт directional information.**
Причины: wash trading, нет истинных инсайдеров, volume inflated leverage,
маркет-мейкеры delta-neutral. Sell%≈50% даже при крупных движениях.

Полезное: инфра (Binance Vision стриминговый парсер, tick bar builder, aggTrades кэш).

## Скрипты

- `scripts/research/cascade_tick_bars.py` — tick vs time bars (3 дня, API)
- `scripts/research/cascade_imbalance_bars.py` — + imbalance bars
- `scripts/research/cascade_exhaustion_exit.py` — exhaustion exit эксперимент
- `scripts/research/cascade_tick_backtest.py` — 90-day backtest (Binance Vision)
- `scripts/research/cascade_exhaustion_exit.py` — exhaustion exit strategies
- `scripts/research/bigmove_tick_filter.py` — tick bars для big move direction
- `scripts/research/informed_flow_detector.py` — absorption/stealth/breakout detection
- `scripts/research/tradfi_tick_shadow.py` — NQ lag + tick bar features
