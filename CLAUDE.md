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
│   ├── core/                # Ядро: конфиг, модели
│   │   ├── config.py        # Pydantic-settings конфиг
│   │   ├── exchange/        # CCXT адаптер (base, manager)
│   │   ├── models/          # Общие модели данных
│   │   └── websocket/       # Real-time данные (placeholder)
│   ├── data/                # Слой данных (placeholder)
│   ├── strategy/            # Логика стратегий
│   │   ├── levels.py        # S/R уровни, D1 multi-TF
│   │   ├── signals.py       # Генерация сигналов
│   │   ├── features.py      # Фичи для ML
│   │   └── models.py        # Dataclass-ы стратегий
│   ├── strategies/          # Конкретные стратегии
│   │   ├── base.py          # Strategy ABC
│   │   ├── miro_strategy.py # Miro (breakout + retest)
│   │   └── volume_ranking.py# Volume Ranking L/S
│   ├── ai/                  # AI-пайплайн
│   │   ├── chart_generator.py # Генерация графиков
│   │   ├── ml_scorer.py     # ML классификатор
│   │   └── vision_scorer.py # Claude Vision скоринг
│   ├── screener/            # Скринер монет
│   │   ├── scanner.py       # Основной сканер
│   │   ├── data_fetcher.py  # Загрузка данных
│   │   ├── coins_in_play.py # Отбор монет
│   │   └── state.py         # Состояние скринера
│   ├── paper_trading/       # Paper trading
│   │   ├── tracker.py       # Трекер сделок
│   │   ├── db.py            # SQLite хранилище
│   │   └── stats.py         # Статистика P&L
│   ├── portfolio/           # Портфельный менеджер
│   │   └── manager.py       # Мульти-стратегия
│   ├── execution/           # Исполнение (placeholder)
│   └── api/
│       └── telegram/        # Telegram бот + алерты
├── scripts/
│   ├── paper_trading.py     # Запуск paper trading
│   ├── daily_report.py      # Ежедневный отчёт
│   └── research/            # Ресёрч-скрипты (~40 файлов)
├── config/
│   └── example.env          # Пример .env
├── tests/                   # Тесты
└── data/                    # Данные (в .gitignore)
    ├── raw/                 # Сырые данные, parquet
    ├── processed/           # Обработанные
    ├── cache/               # Кэш
    ├── models/              # Сохранённые ML-модели
    ├── reports/             # Отчёты бэктестов
    └── logs/                # Логи
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
