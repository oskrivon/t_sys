"""Abstract WebSocket feed interface."""
from __future__ import annotations

from abc import ABC, abstractmethod


class WebSocketFeed(ABC):
    """One per exchange. Connects, subscribes, publishes events to EventBus."""

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def subscribe_tickers(self, symbols: list[str]) -> None: ...

    @abstractmethod
    async def subscribe_executions(self) -> None:
        """Subscribe to private execution/order/position updates."""

    @abstractmethod
    async def run(self) -> None:
        """Main receive loop. Publishes parsed messages to EventBus."""

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...
