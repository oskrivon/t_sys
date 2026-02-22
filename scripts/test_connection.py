#!/usr/bin/env python3
"""Тест подключения к биржам.

Запуск:
    python scripts/test_connection.py

Или с конкретной биржей:
    python scripts/test_connection.py binance
"""

import asyncio
import sys
from decimal import Decimal

import structlog

# Настройка логирования
structlog.configure(
    processors=[
        structlog.dev.ConsoleRenderer(colors=True),
    ],
)
logger = structlog.get_logger()


async def test_exchange(exchange_name: str) -> None:
    """Тестирование подключения к бирже."""
    from src.core.exchange import ExchangeManager
    from src.core.models import Timeframe

    print(f"\n{'='*60}")
    print(f"Testing {exchange_name.upper()}")
    print("=" * 60)

    async with ExchangeManager() as manager:
        try:
            adapter = manager.get(exchange_name)
        except KeyError:
            print(f"❌ {exchange_name} not configured")
            return

        # 1. Информация о бирже
        print("\n📊 Exchange Info:")
        info = await adapter.get_exchange_info()
        print(f"   Name: {info.name}")
        print(f"   Connected: {info.connected}")
        print(f"   Has Spot: {info.has_spot}")
        print(f"   Has Futures: {info.has_futures}")
        print(f"   Symbols count: {len(info.symbols)}")

        # 2. Тикер BTC/USDT
        print("\n💰 BTC/USDT Ticker:")
        try:
            ticker = await adapter.get_ticker("BTC/USDT")
            print(f"   Last: ${ticker.last:,.2f}")
            print(f"   Bid: ${ticker.bid:,.2f}" if ticker.bid else "   Bid: N/A")
            print(f"   Ask: ${ticker.ask:,.2f}" if ticker.ask else "   Ask: N/A")
            print(f"   24h Change: {ticker.change_24h_pct:.2f}%" if ticker.change_24h_pct else "   24h Change: N/A")
        except Exception as e:
            print(f"   ❌ Error: {e}")

        # 3. Стакан
        print("\n📈 Order Book (top 5):")
        try:
            orderbook = await adapter.get_orderbook("BTC/USDT", limit=5)
            print("   Bids:")
            for price, amount in orderbook.bids[:3]:
                print(f"      ${price:,.2f} x {amount:.4f}")
            print("   Asks:")
            for price, amount in orderbook.asks[:3]:
                print(f"      ${price:,.2f} x {amount:.4f}")
            print(f"   Spread: ${orderbook.spread:,.2f}" if orderbook.spread else "   Spread: N/A")
        except Exception as e:
            print(f"   ❌ Error: {e}")

        # 4. Последние свечи
        print("\n🕯️ Last 3 candles (1h):")
        try:
            candles = await adapter.get_candles("BTC/USDT", Timeframe.H1, limit=3)
            for candle in candles:
                emoji = "🟢" if candle.is_bullish else "🔴"
                print(f"   {emoji} {candle.timestamp.strftime('%H:%M')} | "
                      f"O: ${candle.open:,.2f} H: ${candle.high:,.2f} "
                      f"L: ${candle.low:,.2f} C: ${candle.close:,.2f}")
        except Exception as e:
            print(f"   ❌ Error: {e}")

        # 5. Баланс (если есть ключи)
        print("\n💳 Balance:")
        try:
            balances = await adapter.get_balance()
            if balances:
                for balance in balances[:5]:  # Показываем первые 5
                    print(f"   {balance.currency}: {balance.total:.8f} "
                          f"(free: {balance.free:.8f})")
            else:
                print("   No balances (or read-only mode)")
        except Exception as e:
            print(f"   ⚠️ {e} (probably no API keys)")

        print("\n✅ Connection test completed!")


async def test_all_exchanges() -> None:
    """Тестирование всех бирж."""
    exchanges = ["binance", "bybit", "okx"]
    for exchange in exchanges:
        await test_exchange(exchange)


async def main() -> None:
    """Главная функция."""
    if len(sys.argv) > 1:
        exchange = sys.argv[1].lower()
        await test_exchange(exchange)
    else:
        await test_all_exchanges()


if __name__ == "__main__":
    asyncio.run(main())
