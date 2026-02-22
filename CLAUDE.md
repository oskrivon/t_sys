# Project: Trading Infrastructure

Гибкая торговая и аналитическая инфраструктура для криптовалютных бирж с поддержкой бэктестинга, автоматической торговли и AI-анализа.

## Стек

- **Core:** Python 3.11+ (asyncio)
- **Exchange API:** ccxt (async), websockets
- **Database:** TimescaleDB (time-series), Redis (cache)
- **API:** FastAPI
- **AI:** Claude API, OpenAI API
- **Future HFT:** Rust (опционально)

## Документация — маршрутизация

При работе с документами используй СТРОГО эти файлы:

| Что записать | Куда | Формат |
|---|---|---|
| Архитектура, модули, зависимости | `docs/ARCHITECTURE.md` | Обнови нужный раздел |
| Как запустить / задеплоить / починить | `docs/RUNBOOK.md` | Обнови нужный раздел |
| План рефакторинга | `docs/REFACTORING.md` | Новая запись в `## Активный` |
| Новая задача или фича | `docs/PLAN.md` | Добавь в `## TODO` |
| Идея на потом | `docs/PLAN.md` | Добавь в `## Backlog` |
| Новое исследование | `docs/[ТЕМА]_RESEARCH.md` | Создай файл + добавь ссылку в `docs/RESEARCH.md` → Активные |
| Исследование завершено | Перенеси в `docs/archive/`, обнови `docs/RESEARCH.md` — добавь однострочный вывод в Архив |
| Результат / что сделано | `docs/PROGRESS.md` | Новая запись СВЕРХУ с датой |
| Бенчмарк / замер | `docs/PROGRESS.md` | В раздел `## Бенчмарки` |
| Принятое решение | `docs/RESEARCH.md` | В раздел `## Решения` |

## Быстрые команды

Когда я говорю:
- "запиши в план" → `docs/PLAN.md`, раздел TODO
- "в бэклог" → `docs/PLAN.md`, раздел Backlog
- "сохрани исследование" → `docs/RESEARCH.md` + отдельный файл если нужно
- "решение принято" → `docs/RESEARCH.md`, раздел Решения
- "готово" / "сделано" → `docs/PROGRESS.md`
- "замерь" / "бенчмарк" → `docs/PROGRESS.md`, раздел Бенчмарки
- "в архив" → перенеси в `docs/archive/`

## Правила записи

- Новые записи — СВЕРХУ (новое первым)
- Дата в формате YYYY-MM-DD
- НЕ удаляй существующие записи
- Если в PLAN.md больше 30 задач в Done — перенеси в `docs/archive/plan-YYYY-MM.md`
- Если не уверен куда записать — спроси

## Структура проекта

```
trading/
├── CLAUDE.md                 # Этот файл
├── docs/                     # Документация
│   ├── ARCHITECTURE.md       # Архитектура системы
│   ├── PLAN.md              # Задачи и бэклог
│   ├── PROGRESS.md          # Лог прогресса
│   ├── RESEARCH.md          # Исследования и решения
│   ├── RUNBOOK.md           # Инструкции запуска
│   ├── REFACTORING.md       # Планы рефакторинга
│   └── archive/             # Архив документов
├── src/
│   ├── core/                # Ядро системы
│   │   ├── exchange/        # Интеграция с биржами (CCXT)
│   │   ├── websocket/       # Real-time данные
│   │   └── models/          # Общие модели данных
│   ├── data/                # Слой данных
│   │   ├── collectors/      # Сборщики данных
│   │   ├── storage/         # TimescaleDB, Redis
│   │   └── cache/           # Кэширование
│   ├── strategy/            # Стратегии
│   │   ├── backtester/      # Бэктестинг
│   │   ├── patterns/        # Паттерны (из Miro и др.)
│   │   └── signals/         # Генерация сигналов
│   ├── execution/           # Исполнение
│   │   ├── orders/          # Управление ордерами
│   │   ├── risk/            # Риск-менеджмент
│   │   └── arbitrage/       # Арбитражные стратегии
│   ├── ai/                  # AI-анализ
│   │   ├── analyzer/        # Claude/GPT анализ
│   │   └── monitor/         # AI-мониторинг
│   └── api/                 # API и интерфейсы
│       ├── rest/            # FastAPI endpoints
│       ├── telegram/        # Telegram бот
│       └── dashboard/       # Web UI
├── tests/                   # Тесты
├── scripts/                 # Утилиты и скрипты
├── config/                  # Конфигурации
└── data/                    # Данные (в .gitignore)
    ├── raw/                 # Сырые данные
    ├── processed/           # Обработанные
    └── cache/               # Кэш
```

## Приоритетные биржи

1. Binance (спот + фьючерсы)
2. Bybit
3. OKX

## Важные правила кода

- Весь async код через `asyncio`
- Типизация везде (mypy strict)
- Логирование через `structlog`
- Конфиги через `pydantic-settings`
- Секреты ТОЛЬКО через .env
- Тесты: pytest + pytest-asyncio
