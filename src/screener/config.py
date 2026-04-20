"""Screener configuration."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


# Default watchlist — top liquid USDT pairs on Bybit
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT",
    "ADA/USDT", "AVAX/USDT", "LINK/USDT", "DOT/USDT", "SUI/USDT",
    "NEAR/USDT", "OP/USDT", "ARB/USDT", "FIL/USDT", "ATOM/USDT",
    "APT/USDT", "LTC/USDT", "UNI/USDT", "INJ/USDT", "TIA/USDT",
    "SEI/USDT", "FET/USDT", "RENDER/USDT", "IMX/USDT", "STX/USDT",
    "PEPE/USDT", "WIF/USDT", "BONK/USDT", "FLOKI/USDT", "RUNE/USDT",
    "AAVE/USDT", "MKR/USDT", "SNX/USDT", "CRV/USDT", "PENDLE/USDT",
    "JUP/USDT", "ENA/USDT", "TON/USDT", "TAO/USDT", "JTO/USDT",
    "PYTH/USDT", "WLD/USDT", "STRK/USDT", "MANTA/USDT", "TRX/USDT",
    "ALGO/USDT", "SUPER/USDT", "MEME/USDT", "ONDO/USDT", "HBAR/USDT",
]


class ScreenerConfig(BaseSettings):
    """Screener settings loaded from environment."""

    model_config = SettingsConfigDict(env_prefix="SCREENER_")

    # Coins
    base_symbols: list[str] = DEFAULT_SYMBOLS
    use_coins_in_play: bool = True
    volume_ratio_threshold: float = 1.5
    max_coins: int = 60

    # Strategy params
    timeframe: str = "4h"
    level_lookback: int = 200
    level_min_touches: int = 2
    level_tolerance_pct: float = 1.0
    retest_window: int = 15
    min_level_age: int = 20
    rr_ratio: float = 3.0
    trend_sma: int = 50

    # D1 level mode: "none", "d1_filter", "d1_only"
    d1_mode: str = "d1_only"
    d1_level_lookback: int = 120
    d1_level_tolerance_pct: float = 1.5
    d1_min_level_age: int = 5
    d1_proximity_pct: float = 2.0
    d1_candle_limit: int = 150

    # ML
    ml_threshold: float = 0.25
    model_path: str = "data/models/miro_gb_d1.joblib"

    # Vision
    vision_enabled: bool = False
    vision_min_ml_score: float = 0.50
    vision_min_score: int = 8  # only alert if vision score >= this

    # State
    state_file: str = "data/screener_state.json"

    # Timing
    candle_close_delay_sec: int = 30
    fetch_concurrency: int = 10
    fetch_candle_limit: int = 210
