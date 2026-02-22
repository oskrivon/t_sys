# Исследование: Стек для Trading Infrastructure

**Дата:** 2025-02-22
**Статус:** Завершено

## Вопрос

Какой технологический стек выбрать для торговой инфраструктуры с учётом:
1. Возможного арбитража (нужна низкая латентность)
2. AI-анализа
3. Бэктестинга
4. Гибкости и скорости разработки

## Исследование

### Сравнение языков по латентности

| Язык | Latency/tick | GC паузы | Ecosystem | Dev speed |
|------|--------------|----------|-----------|-----------|
| Rust | 6-12 μs | Нет | Растёт | Медленно |
| C++ | 10-15 μs | Нет | Богатый | Медленно |
| Go | 50-100 μs | Есть | Хороший | Быстро |
| Python | 250-500 μs | Есть | Лучший | Очень быстро |

### Реальные задержки крипто-бирж

| Компонент | Latency |
|-----------|---------|
| Binance REST API | 50-100 ms |
| Binance WebSocket | 10-30 ms |
| Биржевой матчинг | 1-10 ms |
| Сеть (datacenter) | 1-5 ms |

**Вывод:** Bottleneck — биржи, не наш код. Оптимизация до μs не даёт профита.

### Кейс: $30k на оптимизацию

> Команда потратила $30,000 и 2 месяца на оптимизацию с 800μs до 200μs.
> Результат: нулевое улучшение hit rate.
> Причина: bottleneck был на стороне биржи.

### Существующие решения

| Проект | Стек | Подход |
|--------|------|--------|
| Freqtrade | Python | Чистый Python, ML через FreqAI |
| NautilusTrader | Python + Rust | Hybrid, Rust core |
| Jesse | Python | Чистый Python |
| HaasOnline | C# | Проприетарный |

### Python библиотеки

- **ccxt** — 100+ бирж, единый API
- **ccxt.async_support** — асинхронная версия
- **websockets** — WS клиент
- **asyncio** — event loop
- **numpy/pandas** — расчёты
- **TimescaleDB** — time-series хранение

## Вывод

**Рекомендация:** Hybrid подход

```
Phase 1: Python (ccxt-async + asyncio)
    │
    ▼ Если упираемся в латентность
    │
Phase 2: Rust для критичных путей
    - WebSocket handlers
    - Order execution
    - Arbitrage detector
```

**Обоснование:**
1. Python позволяет быстро итерировать
2. 500μs vs 10μs не важно когда биржа отвечает 50ms
3. Rust можно добавить точечно через PyO3
4. NautilusTrader показывает жизнеспособность подхода

## Источники

- [Rust vs C++ for trading](https://databento.com/blog/rust-vs-cpp)
- [HFT in Crypto: Reality](https://medium.com/@laostjen/high-frequency-trading-in-crypto-latency-infrastructure-and-reality-594e994132fd)
- [NautilusTrader](https://nautilustrader.io/)
- [Freqtrade](https://github.com/freqtrade/freqtrade)
