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

## Результаты Этапа 1 (preliminary)

**Дата:** 2026-04-17
**Статус:** В процессе — preliminary findings

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

### Важный caveat
Мы видим execution price уже СОСТОЯВШИХСЯ свопов. Многие — след от арбитражных ботов, уже захвативших spread. Реальные НЕЗАПОЛНЕННЫЕ возможности требуют анализа pool state МЕЖДУ свопами.

### Следующие шаги (для следующей сессии)
1. Повторить для Arbitrum — L2, меньше MEV, дешевле gas
2. Pool state analysis — sqrtPriceX96 между свопами vs CEX → реальные окна
3. EOA swap prototype на Arbitrum testnet
4. Оценка MEV-конкуренции на L2

### Отчёт
`data/reports/cex_dex_spread_2026-03-01.md`

## Риски

1. **MEV / front-running** — наши swaps видны в mempool до включения в блок. Простое решение: Arbitrum (sequencer обрабатывает FIFO, минимум MEV на L2).
2. **Gas spikes** — в Ethereum mainnet gas может скакнуть с $2 до $50 за tx. Monitor + max_gas_cap.
3. **Stablecoin risk** — USDC depeg (был March 2023). Monitor price pegging.
4. **Private key theft** — custody на своём wallet, HSM или hardware wallet для production.
5. **Compliance / KYC** — торговля через DEX обычно не требует, но CEX-leg требует.

## Open questions

1. **Subgraph vs direct RPC?** Subgraph быстрее для history queries, RPC для real-time. Для research скорее subgraph.
2. **L2 выбор:** Arbitrum (самый ликвидный) vs Base (быстро растущий, Coinbase-владелец) vs Optimism. Стартуем с Arbitrum.
3. **Uniswap v3 vs Uniswap v4?** v4 скорее всего выйдет в течение 2026, может изменить dynamics. Следим.
4. **Стартовый капитал?** Для L2 slow-arb достаточно $2-10k. Для L1 — $20-50k из-за газа.

## Источники (будут обновляться)

- Uniswap v3 subgraph: https://thegraph.com/explorer/subgraphs?search=uniswap-v3
- Alchemy API docs: https://docs.alchemy.com/
- web3.py docs: https://web3py.readthedocs.io/
- Flashbots docs (на будущее): https://docs.flashbots.net/
