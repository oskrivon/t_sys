# Настройка доступа к биржам

## Обзор

Для работы с биржами нужны API ключи. Это руководство описывает как их получить и настроить безопасно.

**Золотое правило:** НИКОГДА не включать Withdrawals если не абсолютно необходимо.

## Сводная таблица

| Биржа | Credentials | Read | Trade | Withdraw | Testnet |
|-------|-------------|------|-------|----------|---------|
| Binance | Key + Secret | ✅ | ✅ | ❌ | testnet.binance.vision |
| Bybit | Key + Secret | ✅ | ✅ | ❌ | testnet.bybit.com |
| OKX | Key + Secret + **Passphrase** | ✅ | ✅ | ❌ | Demo Trading API |

---

## Binance

### Создание ключей

1. Зайти на [binance.com](https://www.binance.com)
2. Profile → API Management
3. Create API → "System generated"
4. Пройти верификацию (2FA)

### Рекомендуемые разрешения

```
✅ Enable Reading
✅ Enable Spot & Margin Trading
✅ Enable Futures (если торгуем фьючерсы)
❌ Enable Withdrawals (НИКОГДА для ботов)
❌ Enable Vanilla Options (не нужно)
```

### IP Restrictions

- Для разработки: "Unrestricted" или IP вашего сервера
- Для продакшена: Только IP серверов

### Важные нюансы

- Futures API не работает если ключ создан ДО открытия фьючерсного аккаунта
- Неактивные ключи (90 дней) автоматически становятся read-only
- Регулярно ротируйте ключи (каждые 90 дней)

### Testnet

- URL: `https://testnet.binance.vision`
- Отдельная регистрация и ключи
- Бесплатные тестовые монеты

**Ссылки:**
- [API Documentation](https://developers.binance.com/docs/binance-spot-api-docs)
- [Testnet](https://testnet.binance.vision)

---

## Bybit

### Создание ключей

1. Зайти на [bybit.com](https://www.bybit.com)
2. Profile → API
3. Create New Key
4. Выбрать тип: "System-generated API Keys"

### Unified Trading Account (UTA)

Bybit использует единый аккаунт для спота и фьючерсов. Убедитесь что:
- Аккаунт обновлён до UTA
- Средства находятся в Unified Trading Account (не в Funding)

### Рекомендуемые разрешения

```
API Key Type: Read-Write

Unified Trading:
  ✅ Orders
  ✅ Positions

Assets:
  ✅ Account Transfer (для перемещения между счетами)
  ❌ Withdraw (НИКОГДА)
```

### Важные нюансы

- 2FA обязательна перед созданием ключей
- Проверьте что средства в Unified Trading Account

### Testnet

- URL: `https://testnet.bybit.com`
- Отдельная регистрация
- Тестовые монеты через faucet

**Ссылки:**
- [API Documentation](https://bybit-exchange.github.io/docs/v5/intro)
- [Testnet](https://testnet.bybit.com)

---

## OKX

### Создание ключей

1. Зайти на [okx.com](https://www.okx.com)
2. Profile → API
3. Create API

### ⚠️ Passphrase

OKX требует **Passphrase** — дополнительный пароль для API.

**ВАЖНО:**
- Passphrase нельзя восстановить!
- Сохраните его сразу при создании
- Если потеряли — создавайте новый ключ

### Рекомендуемые разрешения

```
✅ Read
✅ Trade
❌ Withdraw (НИКОГДА)
```

### Важные нюансы

- Ключи без IP whitelist + с торговыми правами удаляются через 14 дней неактивности
- Read-only ключи с IP whitelist не истекают

### Demo Trading (Testnet)

OKX использует Demo Trading вместо отдельного testnet:

1. Trade → Demo Trading → Personal Center
2. Demo Trading API → Create Demo Trading API Key
3. В запросах добавлять header: `x-simulated-trading: 1`

**Ссылки:**
- [API Documentation](https://www.okx.com/docs-v5/en/)
- [Demo Trading](https://www.okx.com/demo-trading)

---

## Безопасность

### Хранение ключей

```bash
# НИКОГДА не коммитить в git!
# Использовать .env файл (добавлен в .gitignore)

# .env
BINANCE_API_KEY=xxx
BINANCE_SECRET=xxx
BYBIT_API_KEY=xxx
BYBIT_SECRET=xxx
OKX_API_KEY=xxx
OKX_SECRET=xxx
OKX_PASSPHRASE=xxx
```

### Чек-лист безопасности

- [ ] Withdrawals отключены на всех ключах
- [ ] Ключи не в git репозитории
- [ ] .env в .gitignore
- [ ] 2FA включена на всех биржах
- [ ] IP whitelist настроен (для продакшена)
- [ ] Регулярная ротация ключей

### Что делать если ключ скомпрометирован

1. **Немедленно** удалить ключ на бирже
2. Проверить историю операций
3. Создать новый ключ
4. Обновить .env

---

## Настройка в проекте

После получения ключей:

```bash
# 1. Скопировать шаблон
cp config/example.env .env

# 2. Заполнить ключи
nano .env  # или любой редактор

# 3. Проверить что .env в .gitignore
cat .gitignore | grep .env
```

Пример заполненного `.env`:

```env
# Binance
BINANCE_API_KEY=vmPUZE6mv9SD5VNHk4HlWFsOr6aKE2zvsw0MuIgwCIPy6utIco14y7Ju91duEh8A
BINANCE_SECRET=NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j
BINANCE_TESTNET=true

# Bybit
BYBIT_API_KEY=XXXXXXXXXXXXXXXX
BYBIT_SECRET=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
BYBIT_TESTNET=true

# OKX
OKX_API_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
OKX_SECRET=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
OKX_PASSPHRASE=YourSecurePassphrase123!
OKX_DEMO=true
```
