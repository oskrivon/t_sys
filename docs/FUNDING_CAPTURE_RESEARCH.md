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

## Оптимальная конфигурация

```
Entry: 5 секунд до settlement
Exit: по execType=Funding WS event (hold <1s)
Threshold: >10bps (naked perp), >31bps (spot hedge для крупных позиций)
Leverage: 10x
Символы: dynamic scan 573 пар каждые 5 мин, WS мониторинг ~40 горячих
```
