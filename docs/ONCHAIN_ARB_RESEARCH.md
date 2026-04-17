# On-chain arbitrage — research

**Дата начала:** 2026-04-17
**Статус:** Активное
**Приоритет:** Parallel track (комплементарен funding rate research)

## Цель

Оценить возможности арба на стыке **CEX ↔ DEX** на Ethereum + L2 (Arbitrum, Base, Optimism) **без смарт-контрактов** на нашей стороне. Ответить:

1. Какого размера и частоты CEX-DEX спреды в реальности?
2. Выполнимо ли slow-arb (inventory-based, minutes/hours) без своих контрактов?
3. Где главные узкие места — газ, MEV, timing?
4. Стоит ли в итоге идти в atomic multi-DEX арб (со смарт-контрактом)?

## Определения и рамки

### Что МЫ делаем без смарт-контрактов
- **Price monitoring**: читать pool state, swap events, build spread-feed (EOA/read-only)
- **Simple EOA swaps**: вызов Uniswap/Curve router как обычный юзер
- **Slow CEX-DEX arb**: inventory на обеих сторонах, swap один leg → потом другой leg через transfer/trade

### Что НЕ делаем (требует контракта)
- **Atomic multi-hop** — `USDC→ETH на Uni → USDC на Sushi` за одну транзакцию
- **Flash loans** — занять → арбить → вернуть в одном блоке
- **MEV-protected bundles** через flashbots (технически можно EOA, но контракт эффективнее)

### Классы арба (их реальный статус)

| Класс | Кто доминирует | Для нас |
|---|---|---|
| **DEX-DEX atomic** (Uni vs Sushi, одна сеть) | MEV searchers + builders | Не наш уровень, забываем |
| **CEX-DEX** (Uni vs Binance) | Semi-professionals + market makers | **Реалистично**, slow-arb работает |
| **Cross-chain** (L1 vs L2 одной пары) | Bridging bots | Сложно, рассмотрим позже |
| **Stablecoin peg** (USDC/USDT/DAI расхождения) | MMs | Низкий APR, но низкий риск |

Первый приоритет — **CEX-DEX**.

## Гипотезы

1. **H1:** Между Uniswap v3 (ETH/USDC pool) и Binance ETH/USDT спреды >30bps возникают регулярно (минимум 1-2 раза в час при нормальной волатильности).
2. **H2:** Типичная **длительность** CEX-DEX спреда — **минуты**, а не миллисекунды (ограничено скоростью блока + временем включения транзакции в mempool).
3. **H3:** Slow-arb (pre-funded inventory, no atomicity) экономически жизнеспособен при капитале от $20-50k (из-за газа).
4. **H4:** На L2 (Arbitrum, Base) газ низкий, спреды выше → **L2 > L1** для нашего кейса.

## Scope первого захода

**Сети:** Ethereum mainnet + Arbitrum One
**DEX:** Uniswap v3 (самый ликвидный)
**Пары:** WETH/USDC (пул 0.05% — самый ликвидный), WETH/USDT
**CEX ref:** Binance spot ETH/USDT (уже есть данные)
**Период:** последние 3 месяца (2026-01 — 2026-03)

## Методология

### 1. Данные on-chain

**Источник данных:** The Graph — `uniswap-v3-ethereum` и `uniswap-v3-arbitrum` subgraph.

Что берём:
- Каждый swap event: timestamp, pool, amount0, amount1, sqrtPriceX96 (откуда price), gas used
- Pool state snapshots каждый блок (ликвидность по уровням tick)
- TX hash → можем смотреть mempool inclusion delay через node

Альтернатива: прямое `eth_getLogs` через Alchemy free tier (300M compute units/мес хватит).

### 2. Данные CEX

Используем **наши существующие**:
- Binance trades aggregates — per-second bid/ask proxy (из arb research Этапа 1)
- Tardis free 2026-03-01 для точной сверки

Соответствие tokens:
- WETH on-chain = ETH на CEX (1:1)
- USDC on-chain vs USDT на CEX: depeg risk обычно <5bps, считаем 1:1 как первое приближение

### 3. Построение spread feed

Для каждого swap-блока на DEX:
```
dex_price = (amount_out / amount_in) с учётом pool fee
cex_mid   = (best_bid + best_ask) / 2 в соответствующий timestamp
spread_bps = (dex_price - cex_mid) / cex_mid * 10000
```

Важно: **direction matters** — если DEX дороже CEX, арб это "buy CEX → sell DEX".

### 4. Моделирование slow-arb execution

Симулируем обычное исполнение без atomicity:
```
t=0: видим spread_bps > threshold (скажем 30bps)
t=1: шлём swap на DEX
t=2: swap включён в блок (~12 сек Ethereum, ~1-2 сек Arbitrum)
t=3: шлём order на Binance (off-chain, почти мгновенно)
t=4: order fills (обычно < 1 сек)

Метрики:
  realized_spread = spread_at_fill_t4 - entry_spread_t0
  gas_cost (зависит от сети)
  slippage_dex (зависит от pool depth и нашего размера)
  slippage_cex (обычно малый при top-of-book)
  net_profit = entry_spread - gas - slippage - 2×fee
```

### 5. Статистика

- Число spreads > N bps за период (N = 10, 30, 50, 100)
- Распределение длительности
- Required gas vs profit (L1 vs L2 comparison)
- Frequency по парам, по времени суток, по волатильности

## Этапы работы

### Этап 1 — Research (сейчас, $0)
- [ ] Setup Alchemy account + free RPC endpoint
- [ ] `scripts/research/fetch_uniswap_swaps.py` — загрузка swap events через The Graph (Ethereum + Arbitrum, WETH/USDC и WETH/USDT)
- [ ] Сопоставить с CEX данными (используем Tardis March-1 + наш proxy)
- [ ] Backtest slow-arb strategy с learned parameters
- [ ] Отчёт: `data/reports/onchain_arb_research_{period}.md`
- [ ] **Gate:** реалистично ли slow-arb? L1 vs L2?

### Этап 2 — EOA swap prototype (после Gate 1)
- [ ] web3.py integration, создать кошелёк для тестов
- [ ] Написать `src/core/chain/uniswap_adapter.py` с read-only + execute swap через router
- [ ] Проверка на testnet (Sepolia) или Arbitrum One с малой суммой ($10-50)
- [ ] Замерить **реальные** времена block inclusion, gas, slippage
- [ ] Сверить с моделью из Этапа 1

### Этап 3 — Live slow-arb MVP (после Этапа 2 если зелёный)
- [ ] Pre-funded inventory: $1-5k на Arbitrum + $1-5k на Binance
- [ ] Executor с risk limits (max gas spent/day, min profit threshold)
- [ ] Мониторинг + Telegram alerts
- [ ] Running for 2-4 недель, validation

### Этап 4 — Smart contract arb (**в Backlog, не сейчас**)
- Atomic DEX-DEX через свой контракт
- Flash loans
- MEV protection via flashbots

## Инструменты и стек

| Задача | Инструмент |
|---|---|
| RPC узел | Alchemy (free tier) или Infura |
| Subgraph queries | The Graph — `messari/uniswap-v3-ethereum` или official |
| Web3 client | `web3.py` 7.x |
| Wallet | Кастомный EOA (private key в .env, НЕ в git) |
| DEX integrations | Uniswap V3 Swap Router на mainnet/Arbitrum |
| Gas estimation | `eth_estimateGas` + priority fee oracle |

## Результаты Этапа 1 — Execution price analysis

**Дата:** 2026-04-17
**Статус:** Завершён

### Данные
- 8,080 Uniswap v3 WETH/USDC (0.05% pool) свопов за 2026-03-01 через public RPC
- Сопоставлены с Binance ETH/USDT L1 bookTicker (Tardis free sample)
- Timestamps estimated via block interpolation (±2s accuracy)

### Ключевые результаты

**Spread distribution (DEX execution price vs CEX mid):**
- std = 16 bps, p5/p95 = -20/+23 bps, max = 152 bps
- 45.8% свопов с |spread| > 5 bps
- 25.2% с |spread| > 10 bps, 10.6% > 20 bps, 2.2% > 50 bps

**Profitability (после 15 bps pool+CEX fee, ДО gas):**
- 1,278 / 8,080 свопов (15.8%) profitable
- Median profit: 10.3 bps, p90: 42.4, max: 137.2
- Direction balanced: 660 buy_dex + 618 sell_dex

**После gas:**
- Ethereum L1 ($5 gas), trade $10k: 857 profitable свопов
- Arbitrum ($0.10 gas), trade $1k: 1,176 profitable свопов

### Caveat
Execution price отражает УЖЕ СОСТОЯВШИЕСЯ свопы (часть — арб-боты). Для реальных возможностей нужен pool state analysis.

### Отчёт
`data/reports/cex_dex_spread_2026-03-01.md`

---

## Результаты Этапа 1 — Pool state analysis (ключевой)

**Дата:** 2026-04-17
**Статус:** Завершён для L1, Arbitrum в процессе

### Методология
Цена пула (sqrtPriceX96) МЕЖДУ свопами vs CEX mid, per-second timeline.
Показывает **реальные незанятые окна**, доступные для EOA swap.

### L1 Ethereum — Pool dynamics
- Total swaps: 3,953, unique seconds: 2,248
- 97.4% времени пул не меняет цену (нет свопов)
- Gaps: median=24s, p90=60s, max=6,072s (1.7 часа)

### Staleness effect (КРИТИЧЕСКИЙ finding)

| Time since swap | Mean |spread| | Profitable (>15bps) |
|---:|---:|---:|
| 0-5s | 4.0-4.2 bps | 0.3-0.8% |
| 6-60s | 3.9-4.7 bps | 2.2-3.4% |
| 121-300s | 14.3 bps | 14.6% |
| 301-1000s | **92.3 bps** | **92.4%** |
| 1001+s | **165.9 bps** | **93.1%** |

Вывод: большие спреды (>50 bps) — **stale пул** (никто не торгует).
Без freshness filter APR завышен в 5-10x.

### Реалистичные окна (fresh <= 120s)
- **264 окна/день**, median=3s, mean=5.4s, max=46s
- Net arb: median=2.7 bps, p90=11.6, max=79.5

### Execution timing (CRITICAL)
L1 блок = 12 секунд. Большинство окон **короче блока**:
- >= 12s (1 блок): **34 окна** (12.9%)
- >= 24s (2 блока): **6 окон** (2.3%)

### L1 экономика (реалистичная, capture=20%)

| Trade | Feasible windows | APR (20% capture) |
|---:|---:|---:|
| $5k | 17/day | ~61% |
| $10k | 26/day | ~123% |
| $25k | 34/day | ~180% |

Маргинально, но не нулевое. С учётом slippage и MEV — вероятно ниже.

### Arbitrum — реальные данные (WETH/USDC.e 0.05%)

**Данные:** 9,030 свопов за 2026-03-01 через public RPC (Arbitrum One)
**Pool:** `0xC31E54c7a869B9FcBEcc14363CF510d1c41fa443`

**Pool dynamics:**
- 2.3x больше свопов чем L1 (9,030 vs 3,953)
- Median gap: **3s** (vs 24s на L1, 8x быстрее корректировка)
- 94% времени без свопов (vs 97.4% L1)

**Staleness на Arbitrum:**

| Time since swap | Mean |spread| | Profitable (>15bps) |
|---:|---:|---:|
| 0-5s | 4.4-4.8 bps | 0.4-0.9% |
| 6-60s | 3.2-3.6 bps | 0.2-0.3% |
| 121-300s | 3.8 bps | 0.9% |
| **301+s** | **87.5 bps** | **85.9%** |

**Ключевое отличие:** свежие спреды на Arbitrum МЕНЬШЕ чем на L1 (3-5 bps vs 4-5 bps).
Арб-боты корректируют цену агрессивнее.

**Fresh арб-окна (<=60s):**
- **78 окон/день** (vs 264 на L1 fresh, 3.4x МЕНЬШЕ)
- Duration: **median=2s**, mean=2.5s, max <10s
- **70.7% закрыто swap-ом** (vs 49.4% L1) — больше конкуренция
- Все окна исполнимы (block 0.25s)

**Экономика Arbitrum (fresh, gas $0.10):**

| Trade | Capture 10% | Capture 20% | Capture 30% |
|---:|---:|---:|---:|
| $1k | ~98% APR | ~196% APR | ~294% APR |
| $5k | ~118% APR | ~236% APR | ~354% APR |
| $10k | ~121% APR | ~241% APR | ~362% APR |

### L1 vs Arbitrum (финальное сравнение)

| Метрика | L1 (feasible >=12s) | Arbitrum (fresh) |
|---|---|---|
| Windows/day | 34 | 78 |
| Median duration | 17s | 2s |
| All executable? | 12.9% | 100% |
| Net arb median | 9.9 bps | 2.6 bps |
| Closed by swap | 49.4% | 70.7% |
| APR@$10k, 20% capture | ~123% | **~241%** |

**Вывод:** Arbitrum лучше L1 по APR несмотря на больше конкуренции,
потому что все окна исполнимы (block 0.25s vs 12s).

### Отчёты
- L1: `data/reports/pool_state_eth_2026-03-01.md`
- Arbitrum: `data/reports/pool_state_arb_2026-03-01.md`

---

## Gate onchain-1 — Решение

**Дата:** 2026-04-17
**Статус:** YELLOW (условный зелёный)

### Резюме

| Трек | APR estimate | Капитал | Статус |
|---|---|---|---|
| **On-chain Arbitrum** | **120-360%** при $5-10k | $5-10k | **BEST TRACK** |
| On-chain L1 | 60-180% при $10-25k | $10-25k | Маргинально |
| Cross-exchange CEX | ~20% с colocation | $10k | Tail |
| Funding rate | 3.5-5% passive | Any | Yield product |

### Почему YELLOW а не GREEN

1. **Один день данных** — нужна проверка на 7-30 дней
2. **Capture rate гипотетический** — реальный rate неизвестен без live test
3. **Slippage не моделирован** — может съесть 30-50% profit
4. **Конкуренция на Arbitrum** — 70.7% окон закрыто ботами
5. **Execution latency** — наш EOA approach: detect spread → build tx → submit → ~1-3s

### Решение: переход к Этапу 2

Этап 2 — EOA swap prototype на Arbitrum:
1. web3.py + Arbitrum RPC — read pool state real-time
2. Binance WS для CEX mid real-time
3. Test swap на Arbitrum с малой суммой ($10-50)
4. Измерить реальное: latency, gas, slippage, fill rate
5. Через 1-2 недели данных → Gate onchain-2

### Следующие шаги (Этап 2)
- [ ] web3.py integration, Arbitrum wallet (testnet first)
- [ ] `src/core/chain/uniswap_adapter.py` — pool state reader + swap executor
- [ ] Real-time CEX-DEX spread monitor (WS + pool events)
- [ ] Testnet swap ($10-50) — замерить latency, gas, slippage
- [ ] Live data collection 1-2 недели
- [ ] Gate onchain-2: go/no-go на production

## Риски

1. **MEV / front-running** — на Arbitrum FIFO (sequencer), MEV минимально. Но speed competition всё ещё есть.
2. **Gas spikes** — Arbitrum gas стабильно низкий ($0.05-0.20), но может расти при L1 congestion.
3. **Stablecoin risk** — USDC depeg. Monitor price pegging.
4. **Private key theft** — custody на своём wallet, HSM для production.
5. **Compliance / KYC** — DEX leg не требует, CEX leg требует.
6. **Slippage в concentrated liquidity** — при $10k+ может быть значительным.

## Open questions

1. ~~Subgraph vs direct RPC?~~ **Решено:** direct RPC + eth_getLogs работает, subgraph не нужен.
2. ~~L2 выбор~~ **Решено:** Arbitrum One (подтверждено данными — самый ликвидный).
3. **Uniswap v3 vs v4?** v4 может изменить dynamics. Следим.
4. ~~Стартовый капитал?~~ **Решено:** $5-10k для Arbitrum slow-arb.
5. **USDC vs USDC.e?** Анализировали USDC.e pool. Нативный USDC pool может быть ликвиднее.
6. **Capture rate?** Нужен live тест. Гипотеза: 10-20% реалистично.

## Источники (будут обновляться)

- Uniswap v3 subgraph: https://thegraph.com/explorer/subgraphs?search=uniswap-v3
- Alchemy API docs: https://docs.alchemy.com/
- web3.py docs: https://web3py.readthedocs.io/
- Flashbots docs (на будущее): https://docs.flashbots.net/
