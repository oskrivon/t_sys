# Исследования и решения

## Активные исследования

- [Funding rate arbitrage](FUNDING_ARB_RESEARCH.md) — main track после Gate 2 (2026-04-17)
- [On-chain CEX-DEX arbitrage](ONCHAIN_ARB_RESEARCH.md) — **сильный сигнал**: 15.8% свопов profitable, median 10 bps. Требует pool state analysis (2026-04-17)
- [Cross-exchange арб](ARBITRAGE_RESEARCH.md) — Этапы 1+2 завершены, дальнейшая работа отложена в tail
- [Стек и латентность](STACK_RESEARCH.md) — выбор языка для trading infrastructure
- [Настройка бирж](EXCHANGES_SETUP.md) — API ключи Binance/Bybit/OKX

## Решения

### [Funding rate — yield product, not trading alpha] — 2026-04-17
**Контекст:** Завершён Этап 1 funding research. 20 пар × 3 биржи × 12 месяцев.

**Ключевой результат:** все активные стратегии отрицательны после комиссий. Только buy-and-hold прибылен на 3.5-5% APR на select парах (BTC, ETH, LINK, SUI). Cross-exchange funding arb 0/114 прибыльных.

**Решение:** оставить как passive yield baseline для idle capital на биржах. Не main trading track. Пивот на on-chain CEX-DEX research.

**Детали:** `docs/FUNDING_ARB_RESEARCH.md` → "Результаты Этапа 1", отчёты `data/reports/funding_analysis.md` и `data/reports/funding_backtest_v2.md`

---

### [Gate 2 — Cross-exchange в tail, пивот на funding + on-chain] — 2026-04-17
**Контекст:** Завершён Этап 2 (Tardis L1 валидация free sample на 2026-03-01). Получены честные L1-цифры для сравнения с trades-proxy.

**Цифры:**
- PROXY vs L1 OPT: precision 85-93%, recall 73-93% — proxy валиден
- L1 OPT: BTC 15 окон, ETH 47 окон — **100% окон ≤1 секунды**
- **L1 SNAP = 0 окон** — при опросе раз в секунду не увидеть НИЧЕГО

**Ключевое открытие:** реальная длительность cross-exchange spot арб-окон — **миллисекунды**, не секунды. HFT/MMs закрывают спреды внутри секунды.

**Анализ требуемой инфраструктуры:**
- Rust vs Python: экономит 1-5 ms на CPU, но сеть (50-300 ms) — это 95% latency budget. Rust не решает проблему.
- Прямой кабель Москва→биржа: physically limited скоростью света (~150ms до Токио). Не решение.
- **Colocation в регионе биржи (AWS Tokyo/Singapore/HK)** — правильный подход. VPS $20-200/мес, latency 20-50ms в регионе.
- Market makers (Jump, Wintermute) уже colocated везде с FPGA/C++ — наш реалистичный fill rate 20-40%, не 80%.

**Пересмотренная экономика cross-exchange:**
- 63 окна/сутки × 30% fill × 4 bps × $10k = ~$2k/мес на $10k капитала (~20% APR)
- Приемлемо, но не бест-в-классе

**Решение:** **Cross-exchange WS переносится в tail roadmap-а**. Строить когда готова остальная инфраструктура. Приоритет переключается на:

1. **Funding rate arbitrage** (main) — APR 8-30% без плеча, Python-friendly, без HFT-конкуренции. Новый research-док: `docs/FUNDING_ARB_RESEARCH.md`
2. **On-chain CEX-DEX research** (parallel, $0) — через The Graph + EOA swaps, без смарт-контрактов. Новый: `docs/ONCHAIN_ARB_RESEARCH.md`
3. **Triangular** (side quest) — mini-research на альткоинах, PLAN.md

**Почему:**
- Funding arb работает в горизонте часы-дни — Python на равных, нет гонки latency
- On-chain research полностью бесплатный, хорошо дополняет картину
- Cross-exchange не отменяется, просто не первым, и нужна колокация

**Что не делаем:**
- Не тратим $300 на Tardis paid — trades-proxy валиден
- Не переходим на Rust прежде времени — проблема в сети, не в CPU
- Не строим WS-collector для research — теперь мы знаем, нужен он для production-арба (когда дойдём до Phase 5+)

**Детали:** `docs/ARBITRAGE_RESEARCH.md` → "Результаты Этапа 2", отчёт `data/reports/gate2_proxy_vs_l1_2026-03-01.md`

---

### [Gate 1 — Arbitrage Этап 1 пройден] — 2026-04-16
**Контекст:** Завершён Этап 1 исследования (trades-based proxy) на BTC/USDT + ETH/USDT по Binance/Bybit/OKX spot за март 2026.

**Цифры:**
- BTC: 138 окон, 4.4/сутки, медиана net_bps=4.0, медиана длительности **1с** (max 2с)
- ETH: 563 окна, 18/сутки, медиана net_bps=4.8, медиана длительности **1с** (max 7с)
- 92-96% окон — ровно 1 секунда (на trades-proxy)

**Решение:** **Зелёный свет** на Этап 2 (Tardis валидация, бюджет до $50).

**Почему:**
- Окна существуют, прибыль p99 27-36 bps — окупает round-trip fees
- Trades-proxy — нижняя граница реальности; quoted-окна могут быть значительно длиннее
- Tardis sample за $10-50 разрешит ключевой вопрос "1с на proxy = 1с на L1 или 30с?"
- Стоимость ошибки (месяц WS-инфры впустую) >> $50

**Риски:**
- Медиана 1с — на грани возможного для Python+WS даже с геолокацией
- Net_bps медиана 4-5 — slippage и latency могут съесть всё

**Критерий Gate 2:** после Tardis sample — если L1 окна ≥10с медиана → строим WS под production. Если ≤2с — пивот на triangular/funding/CEX-DEX.

**Детали:** `docs/ARBITRAGE_RESEARCH.md` → раздел "Результаты Этапа 1", отчёт `data/reports/arb_trades_2026-03.md`

---

### [Методология арбитражного исследования] — 2026-04-16
**Контекст:** Нужно оценить прибыльность cross-exchange spot арба (BTC/ETH vs USDT на Binance/Bybit/OKX) и требования к инфраструктуре перед тем как вкладываться в Phase 2-4.

**Исходная гипотеза:** скачать бесплатные L1 bookTicker архивы за последний месяц, прогнать анализ за вечер.

**Probe показал:**
- `data.binance.vision/spot/` — bookTicker **вообще не выкладывают** (только klines/trades)
- `data.binance.vision/futures/um/bookTicker/` — есть **только узкое окно** (март 2024 и около), свежего нет
- Bybit `public.bybit.com` — только daily trades csv.gz (формат по имени файла)
- REST API с ключом — **не даёт** исторических bid/ask ни на одной бирже
- On-chain — CEX-сделки off-chain (БД), только capital flows; orderbook не восстановить

**Варианты рассмотренные:**
1. WS-collector своими силами — $0, но ждать 2-4 недели
2. Tardis.dev — ~$100-300/месяц за L1 по 3 биржам или ~$10-50 за sample
3. Trades-based proxy из bid/ask по `isBuyerMaker/side` — coarse но бесплатно
4. Гибрид: сначала coarse, потом Tardis, потом WS

**Решение:** Вариант 4 — последовательный pipeline с gates:
1. **Этап 1 ($0):** trades-based proxy, coarse ответ H1
2. **Gate 1:** сигнал положительный → Этап 2; отрицательный → пивот (triangular/funding/CEX-DEX)
3. **Этап 2 ($10-50):** Tardis sample, L1-валидация proxy
4. **Gate 2:** L1 подтверждает → Этап 3; нет → пересмотр методологии
5. **Этап 3:** WS-collector — уже **не ради research**, а под production (Phase 2)

**Почему:**
- Каждый шаг валидирует предыдущий, можем остановиться на любом
- Самое дорогое (WS-стек + OMS + risk) строим только после green gate
- Не тратим 3 недели на WS с риском «а арба нет» в результатах
- Tardis не заменяет WS — WS нужен под live-торговлю, research это не снимает

**Детали:** `docs/ARBITRAGE_RESEARCH.md`

### [Основной стек] — 2025-02-22
**Контекст:** Нужна гибкая торговая инфраструктура для CEX арбитража
**Варианты:**
1. Python (ccxt + asyncio) — быстрая разработка, 250-500μs латентность
2. Rust — 6-12μs, но долгая разработка
3. Go — 50-100μs, компромисс
4. Hybrid (Python + Rust core) — лучшее из обоих миров

**Решение:** Python для MVP, Rust для критичных путей если понадобится
**Почему:**
- Крипто-биржи сами имеют latency 50-100ms — наши 500μs не bottleneck
- Быстрая итерация важнее micro-optimization
- NautilusTrader показывает что hybrid работает
- Можно постепенно выносить hot paths в Rust

### [Приоритетные биржи] — 2025-02-22
**Контекст:** Какие биржи поддерживать первыми
**Варианты:** Binance, Bybit, OKX, Kraken, Coinbase, Huobi
**Решение:** Binance, Bybit, OKX
**Почему:**
- Наибольшая ликвидность
- Лучшие API
- Основные для арбитража

### [DEX интеграция] — 2025-02-22
**Контекст:** Нужно ли поддерживать ончейн биржи сразу
**Решение:** Отложить на Phase 2
**Почему:**
- Другая модель (пулы vs orderbook)
- Латентность в секундах (блоки)
- MEV/flashbots — отдельная экспертиза
- CEX проще для валидации архитектуры

## Архив

<!-- Формат: - [Тема](archive/ТЕМА_RESEARCH.md) — однострочный вывод/результат -->
