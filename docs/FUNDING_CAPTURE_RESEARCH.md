# Funding Capture — Deep Research

## Дата: 2026-04-20

## Стратегия

Входим в позицию за 5 сек до funding settlement, получаем funding payment, выходим по `execType=Funding` WS event (~0.5 сек после settlement).

**Direction:** LONG если funding < 0 (шорты платят лонгам), SHORT если > 0.
**Leverage:** 10x.
**Exit:** по факту начисления (WS private: `execType=Funding`), timeout 30s safety.

## Бэктест (81 день, 30 символов)

| Threshold | Events | Events/day | WR | Monthly на $15 |
|-----------|--------|------------|-----|----------------|
| >10bps | 579 | 7.1 | 96% | $8.52 (57%/mo) |
| >15bps | 458 | 5.7 | 100% | $8.47 |
| >20bps | 374 | 4.6 | 100% | $8.28 |
| >30bps | 278 | 3.4 | 100% | $7.79 |

**WR 96-100%** — funding payment almost always exceeds fees.
Fees = 2 × 0.055% = 11bps roundtrip.

## Latency Sensitivity

| Hold time | Price risk ($10 not.) | Funding | Net |
|-----------|-----------------------|---------|-----|
| 5s (ideal) | $0.025 | $0.051 | +$0.015 |
| 15s (timer) | $0.044 | $0.051 | -$0.004 |
| 30s (slow) | $0.062 | $0.051 | -$0.022 |

**Критично:** при hold >15 секунд медианный price impact съедает funding.
Решение: exit по `execType=Funding` WS event → hold <1 секунда.

## Live тест (2026-04-20)

**12:00 UTC settlement:**
- 13 монет scheduled, 9 позиций открыто
- Баги: duplicate signals (fixed), OrderType Enum crash (fixed)
- Clean estimate: +$0.037 (+0.25% на $15)

**16:00 UTC settlement:**
- 14 позиций открыто, exit не сработал (Enum crash — fixed)
- Clean estimate: +$0.051 (+0.34% на $15)

## Ценовое поведение вокруг settlement

**92 события, 12 монет, 30 дней:**

| Период | Mean | Median | % Win |
|--------|------|--------|-------|
| 5 мин ДО | -0.35% | +0.05% | 60% |
| 1 мин ДО | -0.36% | -0.03% | 40% |
| 1 мин ПОСЛЕ | -0.11% | -0.25% | 20% |
| 5 мин ПОСЛЕ | -0.54% | -0.58% | **0%** |

**Dump после settlement стабилен:** 100% событий показывают падение через 5 мин.
Те кто набирал позицию для funding — сливают.

### Dump trade (бонусная стратегия, backlog)

Short после exit на 5 мин: +0.15% net после fees, WR 57%.
На $15 — спички. На $1k+ — $315/мес дополнительно. StdDev 1.06% — шумно.
**Статус: backlog, реализовать при scale up.**

## Cross-exchange funding hedge

| Пара | Avg spread | >10bps events | Monthly на $100 |
|------|-----------|--------------|-----------------|
| SUPER | 2.9bps | 11 | $0.85 |
| Остальные | <1.5bps | 0 | $0 |

Fee: 4 ноги × 0.055% = 22bps. Spread редко > 22bps.
**DEAD для текущих объёмов.**

## Selective spot hedge

Buy spot + short perp → delta neutral → collect funding → close both.

| Threshold | Events/day | WR | Monthly на $100 |
|-----------|-----------|-----|-----------------|
| >25bps (taker) | 1.7 | 82% | $15.11 |
| >31bps (taker) | 1.4 | **100%** | **$15.31** |
| >40bps | 1.0 | 100% | $14.78 |

Fee roundtrip: spot 0.1% + futures 0.055% × 2 sides = 31bps.
**100% WR при >31bps, но в 2 раза меньше opportunity set vs naked.**

## Часовые фандинги

Некоторые монеты (RAVE, RDNT) имеют 4-часовой funding interval → 6 settlements/день.
API не возвращает `fundingInterval` поле, определяется по фактическим timestamps.
**Больше opportunities, но rates обычно меньше.**

## Границы применимости (что убьёт стратегию)

1. **Рынок станет neutral/bullish** → funding rates сожмутся к 0. Главный risk.
2. **Конкуренция** → front-run перед settlement вырастет. Пока слабый (данные: 60% positive before, шум).
3. **Биржа изменит модель** → capping extreme rates. Маловероятно (это доход биржи).
4. **Slippage при scale** → $5-10k потолок на low-liquidity coins. BTC/ETH — больше.
5. **Latency** → если exit >15 секунд, стратегия убыточна. Нужен WS event exit.

## Live тест — полный цикл (20:00 UTC, 2026-04-20)

Первый полностью автоматический цикл: entry → funding credited → exit.

**Timeline:**
```
19:59:55  6 signals emitted
19:59:57  6 entries filled
19:59:58  6 exits waiting for execType=Funding
20:00:00  ws_funding_credited → exit executing (все 6)
20:00:00  все позиции закрыты. Hold time: 2-3 секунды.
```

**0 открытых позиций после. 0 ошибок.**

## Экономика при масштабировании

Exact data: 6 trades, 20:00 UTC settlement, rates 10-26bps.

### На $1k (10x leverage)

| Монета | Rate | Funding | Fee | Net |
|--------|------|---------|-----|-----|
| PIEVERSE | 26.5bps | $4.41 | $1.83 | +$2.58 |
| KERNEL | 25.0bps | $4.18 | $1.83 | +$2.34 |
| PORTAL | 23.7bps | $3.95 | $1.83 | +$2.12 |
| DBR | 20.9bps | $3.48 | $1.83 | +$1.65 |
| PLAYSOUT | 14.1bps | $2.34 | $1.83 | +$0.51 |
| NOM | 10.3bps | $1.72 | $1.83 | -$0.12 |
| **TOTAL** | | **$20.08** | **$11.00** | **+$9.08** |

Per settlement: **+$9.08**, monthly (x3/day): **~$817**

> **UPDATE 2026-05-07:** Эти цифры — теоретический максимум на 6 trades одного settlement БЕЗ slippage.
> Live данные (76 trades, Apr 22 — May 5) показали: slippage съедает ~90% funding дохода.
> Реальный PnL при $25 notional / Bybit: **~+$5/мес** (не $817).
> При $1k notional на Bybit: убыточно (95% trades превышают L5 depth).
> Путь к масштабированию: Binance (книги 3-100x глубже, fees 8 vs 11 bps).
> Подробнее: `docs/PROGRESS.md` запись 2026-05-07.

### С максимальным плечом (12-25x по монете)

| Capital | Per settlement | Monthly | w/ referral -30% fee |
|---------|---------------|---------|---------------------|
| $1k | +$19 | $1,711 | $2,317 |
| $10k | +$190 | $17,108 | $23,172 |

> **UPDATE 2026-05-07:** Цифры выше НЕ учитывают slippage и нереалистичны.
> Макс. безопасное плечо: **10x** (max наблюдённый move за hold = 5.96%, ликвидация 20x при 5%).
> Макс. notional на Bybit: **$25** ($50 уже убыточно из-за thin books).

### Breakeven

Fee roundtrip = 2 * 0.055% = 11bps. Trades с rate <11bps убыточны.
С referral (-30%): breakeven = 7.7bps.

### Ликвидация

| Leverage | Ликвидация при | Risk за 3 сек hold |
|----------|----------------|-------------------|
| 10x | ~10% move | SAFE (max observed 5.96%) |
| 15x | ~6.7% move | RISKY (margin 0.7%) |
| 20x | ~5% move | DANGER (max move > liq dist) |
| 25x | ~4% move | DANGER |
| 100x (BTC) | ~1% move | DANGER |

## Оптимальная конфигурация

```
Entry: 5 секунд до settlement
Exit: по execType=Funding WS event (hold <1s)
Threshold: >10bps (naked perp), >31bps (spot hedge для крупных позиций)
Leverage: 10x
Символы: dynamic scan 573 пар каждые 5 мин, WS мониторинг ~40 горячих
```
