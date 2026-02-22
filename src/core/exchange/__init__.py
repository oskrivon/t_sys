"""Exchange adapters - интеграция с биржами через CCXT."""

from src.core.exchange.base import ExchangeAdapter
from src.core.exchange.ccxt_adapter import (
    CCXTAdapter,
    create_binance_adapter,
    create_bybit_adapter,
    create_okx_adapter,
)
from src.core.exchange.manager import (
    ExchangeManager,
    close_exchange_manager,
    get_exchange_manager,
)

__all__ = [
    "CCXTAdapter",
    "ExchangeAdapter",
    "ExchangeManager",
    "close_exchange_manager",
    "create_binance_adapter",
    "create_bybit_adapter",
    "create_okx_adapter",
    "get_exchange_manager",
]
