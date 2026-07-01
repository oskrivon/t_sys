# Astrology vs BTC: полный тест

**Дата:** 2026-05-23
**Статус:** DEAD END
**Гипотеза:** retail-трейдеры (особенно мнительные знаки) читают гороскопы и торгуют по ним, создавая measurable patterns в BTC.

## Что протестировано

### 1. Mercury Retrograde vs BTC (3202 дней, 2017-2026)

| Метрика | Retrograde (643 дня) | Direct (2559 дней) |
|---|---|---|
| Mean daily return | +0.081% | +0.171% |
| Sharpe (approx) | 0.43 | 0.93 |
| Positive days | 50.4% | 51.4% |

28 ретроградных периодов: 13/28 положительных (46%), медиана -0.63%.
Ретроград выиграл только в 3/10 лет. Слабый negative bias, но не значимый.

### 2. Lunar Phases (108 full moons, 109 new moons)

| Phase | Mean return (+-3d) | Positive % |
|---|---|---|
| Full Moon | +1.00% | 59.3% |
| New Moon | +0.64% | 52.3% |

T-test: p=0.77 — **не значимо**. Чистый шум.

### 3. Google Trends vs BTC (454 недели)

| Keyword | Corr vs Return | Corr vs Volatility | Predictive |
|---|---|---|---|
| "mercury retrograde" | r=-0.07, p=0.13 | r=+0.05, p=0.29 | NO |
| **"horoscope"** | r=+0.05, p=0.25 | **r=+0.27, p<0.001** | **Vol only** |
| "astrology" | r=-0.01, p=0.83 | r=-0.08, p=0.08 | NO |

**"horoscope" как Google Trends keyword коррелирует с волатильностью** (r=0.27 same-week, r=0.28 predictive, оба p<0.001). Но это прокси retail attention — люди гуглят гороскопы когда активны на рынках. Не каузальная связь.

### 4. Zodiac Transit Scores vs BTC (3201 день, все 12 знаков)

Рассчитан ежедневный астро-скор для каждого знака по транзитам (Moon, Mercury, Venus, Mars, Jupiter, Saturn) с учётом аспектов (conjunction, sextile, square, trine, opposition) и весов планет (benefics/malefics).

| Sign | Corr | p-value | Predictive |
|---|---|---|---|
| Sagittarius (best) | -0.013 | 0.45 | NO |
| Cancer (мнительный) | -0.004 | 0.81 | NO |
| Pisces (мистик) | -0.009 | 0.61 | NO |
| ... все остальные | <0.013 | >0.45 | NO |

**Ни один знак из 12 не показал значимой связи.** Combined Fear Index (Cancer + Pisces + Scorpio) — r=-0.004, p=0.84.

## Вывод

Гипотеза мёртвая. Даже если retail-трейдеры читают гороскопы, их действия не создают детектируемый паттерн в BTC. Единственный живой сигнал — Google Trends "horoscope" как прокси retail attention для волатильности, но это не астрология, а поведенческий индикатор (аналогично "how to buy bitcoin").

## Скрипты

- `scripts/research/astro_btc_backtest.py` — Mercury Retrograde + Lunar Phases
- `scripts/research/astro_gtrends_btc.py` — Google Trends correlation
- `scripts/research/astro_zodiac_trading.py` — Zodiac transit scores (all 12 signs)
- Raw data: `data/reports/zodiac_daily_scores.csv`, `data/reports/gtrends_mercury_btc.csv`
