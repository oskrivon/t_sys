"""Базовые модели данных для всей системы."""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class Exchange(str, Enum):
    """Поддерживаемые биржи."""

    BINANCE = "binance"
    BYBIT = "bybit"
    OKX = "okx"
    # Tier-3 venues — used only by the icebreaker (wall-eating) data collector.
    # Market-data only; not wired into trading/execution paths. Values are ccxt ids.
    KUCOIN = "kucoinfutures"
    MEXC = "mexc"
    BITGET = "bitget"


class OrderSide(str, Enum):
    """Сторона ордера."""

    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    """Тип ордера."""

    MARKET = "market"
    LIMIT = "limit"
    STOP_LOSS = "stop_loss"
    STOP_LOSS_LIMIT = "stop_loss_limit"
    TAKE_PROFIT = "take_profit"
    TAKE_PROFIT_LIMIT = "take_profit_limit"


class OrderStatus(str, Enum):
    """Статус ордера."""

    PENDING = "pending"
    OPEN = "open"
    CLOSED = "closed"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REJECTED = "rejected"


class PositionSide(str, Enum):
    """Сторона позиции (для фьючерсов)."""

    LONG = "long"
    SHORT = "short"
    BOTH = "both"  # Hedge mode


class MarketType(str, Enum):
    """Тип рынка."""

    SPOT = "spot"
    FUTURES = "futures"
    MARGIN = "margin"
    SWAP = "swap"


class Timeframe(str, Enum):
    """Таймфреймы для свечей."""

    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H2 = "2h"
    H4 = "4h"
    H6 = "6h"
    H8 = "8h"
    H12 = "12h"
    D1 = "1d"
    D3 = "3d"
    W1 = "1w"
    MO1 = "1M"


# =============================================================================
# Базовые модели
# =============================================================================


class BaseModelConfig(BaseModel):
    """Базовая конфигурация для всех моделей."""

    class Config:
        use_enum_values = True
        validate_assignment = True
        extra = "ignore"


class Ticker(BaseModelConfig):
    """Тикер — текущая цена и статистика."""

    exchange: Exchange
    symbol: str
    timestamp: datetime

    last: Decimal = Field(description="Последняя цена")
    bid: Optional[Decimal] = Field(default=None, description="Лучший bid")
    ask: Optional[Decimal] = Field(default=None, description="Лучший ask")

    high_24h: Optional[Decimal] = Field(default=None, description="Максимум за 24ч")
    low_24h: Optional[Decimal] = Field(default=None, description="Минимум за 24ч")
    volume_24h: Optional[Decimal] = Field(default=None, description="Объём за 24ч")
    change_24h_pct: Optional[Decimal] = Field(default=None, description="Изменение за 24ч в %")

    @property
    def spread(self) -> Optional[Decimal]:
        """Спред между bid и ask."""
        if self.bid and self.ask:
            return self.ask - self.bid
        return None

    @property
    def spread_pct(self) -> Optional[Decimal]:
        """Спред в процентах."""
        if self.bid and self.ask and self.bid > 0:
            return (self.ask - self.bid) / self.bid * 100
        return None


class Candle(BaseModelConfig):
    """OHLCV свеча."""

    exchange: Exchange
    symbol: str
    timeframe: Timeframe
    timestamp: datetime

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @field_validator("high")
    @classmethod
    def high_gte_low(cls, v: Decimal, info) -> Decimal:
        """High должен быть >= low."""
        if "low" in info.data and v < info.data["low"]:
            raise ValueError("high must be >= low")
        return v

    @property
    def is_bullish(self) -> bool:
        """Бычья свеча (закрытие выше открытия)."""
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        """Медвежья свеча (закрытие ниже открытия)."""
        return self.close < self.open

    @property
    def body_size(self) -> Decimal:
        """Размер тела свечи."""
        return abs(self.close - self.open)

    @property
    def range_size(self) -> Decimal:
        """Полный диапазон свечи."""
        return self.high - self.low


class OrderBook(BaseModelConfig):
    """Стакан ордеров."""

    exchange: Exchange
    symbol: str
    timestamp: datetime

    bids: list[tuple[Decimal, Decimal]] = Field(
        default_factory=list,
        description="Список [price, amount] bid ордеров"
    )
    asks: list[tuple[Decimal, Decimal]] = Field(
        default_factory=list,
        description="Список [price, amount] ask ордеров"
    )

    @property
    def best_bid(self) -> Optional[Decimal]:
        """Лучший bid (максимальная цена покупки)."""
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[Decimal]:
        """Лучший ask (минимальная цена продажи)."""
        return self.asks[0][0] if self.asks else None

    @property
    def spread(self) -> Optional[Decimal]:
        """Спред."""
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return None

    @property
    def mid_price(self) -> Optional[Decimal]:
        """Средняя цена."""
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return None

    def get_depth(self, side: OrderSide, depth: int = 10) -> list[tuple[Decimal, Decimal]]:
        """Получить глубину стакана."""
        if side == OrderSide.BUY:
            return self.bids[:depth]
        return self.asks[:depth]


class Trade(BaseModelConfig):
    """Сделка (исполненный ордер)."""

    id: str
    exchange: Exchange
    symbol: str
    timestamp: datetime

    side: OrderSide
    price: Decimal
    amount: Decimal
    cost: Decimal = Field(description="price * amount")

    fee: Optional[Decimal] = None
    fee_currency: Optional[str] = None

    order_id: Optional[str] = None
    taker_or_maker: Optional[str] = None  # "taker" или "maker"


class Order(BaseModelConfig):
    """Ордер."""

    id: Optional[str] = None
    client_order_id: Optional[str] = None
    exchange: Exchange
    symbol: str
    timestamp: datetime

    type: OrderType
    side: OrderSide
    status: OrderStatus = OrderStatus.PENDING

    price: Optional[Decimal] = Field(default=None, description="Цена (для лимитных)")
    amount: Decimal = Field(description="Количество")
    filled: Decimal = Field(default=Decimal("0"), description="Исполнено")
    remaining: Optional[Decimal] = Field(default=None, description="Осталось")

    cost: Optional[Decimal] = Field(default=None, description="Стоимость исполненной части")
    average: Optional[Decimal] = Field(default=None, description="Средняя цена исполнения")

    # Stop orders
    stop_price: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None

    # Futures
    leverage: Optional[int] = None
    position_side: Optional[PositionSide] = None
    reduce_only: bool = False

    # Fees
    fee: Optional[Decimal] = None
    fee_currency: Optional[str] = None

    # Связанные сделки
    trades: list[Trade] = Field(default_factory=list)

    @property
    def is_open(self) -> bool:
        """Ордер активен."""
        return self.status in (OrderStatus.PENDING, OrderStatus.OPEN)

    @property
    def is_filled(self) -> bool:
        """Ордер полностью исполнен."""
        return self.filled >= self.amount

    @property
    def fill_pct(self) -> Decimal:
        """Процент исполнения."""
        if self.amount > 0:
            return (self.filled / self.amount) * 100
        return Decimal("0")


class Balance(BaseModelConfig):
    """Баланс по валюте."""

    exchange: Exchange
    currency: str
    timestamp: datetime

    free: Decimal = Field(description="Доступно для торговли")
    used: Decimal = Field(default=Decimal("0"), description="Заблокировано в ордерах")
    total: Decimal = Field(description="Всего")

    # Для фьючерсов
    unrealized_pnl: Optional[Decimal] = None


class Position(BaseModelConfig):
    """Позиция (для фьючерсов)."""

    exchange: Exchange
    symbol: str
    timestamp: datetime

    side: PositionSide
    size: Decimal = Field(description="Размер позиции")
    entry_price: Decimal = Field(description="Цена входа")

    leverage: int = 1
    margin: Optional[Decimal] = None
    margin_type: Optional[str] = None  # "cross" или "isolated"

    unrealized_pnl: Optional[Decimal] = None
    realized_pnl: Optional[Decimal] = None
    liquidation_price: Optional[Decimal] = None

    @property
    def notional_value(self) -> Decimal:
        """Номинальная стоимость позиции."""
        return self.size * self.entry_price

    @property
    def is_long(self) -> bool:
        """Длинная позиция."""
        return self.side == PositionSide.LONG

    @property
    def is_short(self) -> bool:
        """Короткая позиция."""
        return self.side == PositionSide.SHORT


# =============================================================================
# Вспомогательные модели
# =============================================================================


class Symbol(BaseModelConfig):
    """Информация о торговой паре."""

    exchange: Exchange
    symbol: str  # Унифицированный формат: BTC/USDT
    base: str  # BTC
    quote: str  # USDT

    market_type: MarketType = MarketType.SPOT
    active: bool = True

    # Лимиты
    min_amount: Optional[Decimal] = None
    max_amount: Optional[Decimal] = None
    min_cost: Optional[Decimal] = None
    min_price: Optional[Decimal] = None
    max_price: Optional[Decimal] = None

    # Precision
    price_precision: Optional[int] = None
    amount_precision: Optional[int] = None

    # Fees
    taker_fee: Optional[Decimal] = None
    maker_fee: Optional[Decimal] = None


class ExchangeInfo(BaseModelConfig):
    """Информация о бирже."""

    exchange: Exchange
    name: str
    connected: bool = False
    timestamp: Optional[datetime] = None

    # Rate limits
    rate_limit: Optional[int] = None  # Requests per minute

    # Capabilities
    has_spot: bool = True
    has_futures: bool = False
    has_margin: bool = False

    symbols: list[Symbol] = Field(default_factory=list)
