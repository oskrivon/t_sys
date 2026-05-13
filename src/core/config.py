"""Конфигурация приложения через pydantic-settings."""

from enum import Enum
from functools import lru_cache
from typing import Optional

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    """Окружение приложения."""

    DEVELOPMENT = "development"
    PRODUCTION = "production"
    TESTING = "testing"


class ExchangeConfig(BaseSettings):
    """Базовый конфиг для биржи."""

    api_key: Optional[SecretStr] = None
    secret: Optional[SecretStr] = None
    testnet: bool = True

    @property
    def is_configured(self) -> bool:
        """Проверяет что ключи заданы."""
        return self.api_key is not None and self.secret is not None


class BinanceConfig(ExchangeConfig):
    """Конфигурация Binance."""

    model_config = SettingsConfigDict(env_prefix="BINANCE_")

    # Дополнительные настройки Binance
    enable_futures: bool = False
    recv_window: int = 5000  # Окно для timestamp в мс


class BybitConfig(ExchangeConfig):
    """Конфигурация Bybit."""

    model_config = SettingsConfigDict(env_prefix="BYBIT_")

    # Bybit специфичные настройки
    account_type: str = "UNIFIED"  # UNIFIED или CONTRACT


class OKXConfig(ExchangeConfig):
    """Конфигурация OKX."""

    model_config = SettingsConfigDict(env_prefix="OKX_")

    passphrase: Optional[SecretStr] = None
    demo: bool = True  # Demo trading mode

    @property
    def is_configured(self) -> bool:
        """OKX требует passphrase."""
        return super().is_configured and self.passphrase is not None


class DatabaseConfig(BaseSettings):
    """Конфигурация базы данных."""

    model_config = SettingsConfigDict(env_prefix="")

    database_url: str = "postgresql://trading:trading@localhost:5432/trading"
    redis_url: str = "redis://localhost:6379/0"

    # Pool settings
    db_pool_size: int = 5
    db_max_overflow: int = 10


class AIConfig(BaseSettings):
    """Конфигурация AI сервисов."""

    model_config = SettingsConfigDict(env_prefix="")

    anthropic_api_key: Optional[SecretStr] = None
    openai_api_key: Optional[SecretStr] = None

    # Model preferences
    default_model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 4096


class TelegramConfig(BaseSettings):
    """Конфигурация Telegram бота."""

    model_config = SettingsConfigDict(env_prefix="TELEGRAM_")

    bot_token: Optional[SecretStr] = None
    chat_id: Optional[str] = None

    @property
    def is_configured(self) -> bool:
        """Проверяет что бот настроен."""
        return self.bot_token is not None and self.chat_id is not None


class TradingConfig(BaseSettings):
    """Настройки торговли и риск-менеджмента."""

    model_config = SettingsConfigDict(env_prefix="TRADING_")

    # Risk management
    max_position_size_pct: float = Field(default=5.0, ge=0.1, le=100.0)
    max_daily_loss_pct: float = Field(default=5.0, ge=0.5, le=50.0)
    max_drawdown_pct: float = Field(default=15.0, ge=1.0, le=50.0)
    risk_per_trade_pct: float = Field(default=1.0, ge=0.1, le=10.0)
    default_leverage: int = Field(default=1, ge=1, le=125)

    # Order settings
    default_slippage_pct: float = Field(default=0.1, ge=0.0, le=5.0)
    order_timeout_seconds: int = Field(default=30, ge=5, le=300)

    # Paper trading
    paper_trading: bool = True
    paper_trading_balance: float = 10000.0


class Settings(BaseSettings):
    """Главный класс настроек приложения."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App settings
    environment: Environment = Environment.DEVELOPMENT
    debug: bool = False
    log_level: str = "INFO"

    # Exchanges
    binance: BinanceConfig = Field(default_factory=BinanceConfig)
    bybit: BybitConfig = Field(default_factory=BybitConfig)
    okx: OKXConfig = Field(default_factory=OKXConfig)

    # Infrastructure
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)

    # Trading
    trading: TradingConfig = Field(default_factory=TradingConfig)

    # Screener (imported lazily to avoid circular deps)
    @property
    def screener(self):
        from src.screener.config import ScreenerConfig
        return ScreenerConfig()

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Валидация уровня логирования."""
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid_levels:
            raise ValueError(f"log_level must be one of {valid_levels}")
        return upper

    def get_configured_exchanges(self) -> list[str]:
        """Возвращает список настроенных бирж."""
        exchanges = []
        if self.binance.is_configured:
            exchanges.append("binance")
        if self.bybit.is_configured:
            exchanges.append("bybit")
        if self.okx.is_configured:
            exchanges.append("okx")
        return exchanges


@lru_cache
def get_settings() -> Settings:
    """Получить singleton настроек."""
    return Settings()


# Удобный алиас
settings = get_settings()
