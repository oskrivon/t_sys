"""Tests for src/core/config.py — pydantic settings validation."""
from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from src.core.config import (
    ExchangeConfig,
    OKXConfig,
    Settings,
    TradingConfig,
)


# ---------------------------------------------------------------------------
# TradingConfig field validation
# ---------------------------------------------------------------------------

class TestTradingConfigValidation:
    def test_defaults_are_valid(self):
        cfg = TradingConfig()
        assert cfg.default_leverage == 1
        assert cfg.max_position_size_pct == 5.0

    @pytest.mark.parametrize("bad_leverage", [0, -1, 126])
    def test_invalid_leverage_raises(self, bad_leverage):
        with pytest.raises(ValidationError):
            TradingConfig(default_leverage=bad_leverage)

    def test_leverage_at_boundaries(self):
        assert TradingConfig(default_leverage=1).default_leverage == 1
        assert TradingConfig(default_leverage=125).default_leverage == 125

    @pytest.mark.parametrize("bad_pct", [0.0, 0.05, 100.1, -5.0])
    def test_invalid_max_position_size_pct(self, bad_pct):
        with pytest.raises(ValidationError):
            TradingConfig(max_position_size_pct=bad_pct)

    @pytest.mark.parametrize("bad_pct", [0.5, 0.0, 100.1, -1.0])
    def test_invalid_max_daily_loss_pct(self, bad_pct):
        with pytest.raises(ValidationError):
            TradingConfig(max_daily_loss_pct=bad_pct)

    @pytest.mark.parametrize("bad_slip", [-0.1, 5.1])
    def test_invalid_slippage_pct(self, bad_slip):
        with pytest.raises(ValidationError):
            TradingConfig(default_slippage_pct=bad_slip)

    def test_slippage_zero_is_valid(self):
        cfg = TradingConfig(default_slippage_pct=0.0)
        assert cfg.default_slippage_pct == 0.0


# ---------------------------------------------------------------------------
# Settings — log_level validation
# ---------------------------------------------------------------------------

class TestLogLevelValidation:
    @pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    def test_valid_log_levels(self, level):
        s = Settings(log_level=level)
        assert s.log_level == level

    def test_log_level_case_insensitive(self):
        s = Settings(log_level="debug")
        assert s.log_level == "DEBUG"

    def test_invalid_log_level(self):
        with pytest.raises(ValidationError):
            Settings(log_level="TRACE")


# ---------------------------------------------------------------------------
# SecretStr doesn't leak
# ---------------------------------------------------------------------------

class TestSecretFields:
    def test_api_key_hidden_in_repr(self):
        cfg = ExchangeConfig(api_key=SecretStr("my-secret-key"), secret=SecretStr("s"))
        r = repr(cfg)
        assert "my-secret-key" not in r

    def test_api_key_hidden_in_str(self):
        cfg = ExchangeConfig(api_key=SecretStr("super-secret"), secret=SecretStr("s"))
        s = str(cfg)
        assert "super-secret" not in s

    def test_get_secret_value(self):
        cfg = ExchangeConfig(api_key=SecretStr("the-key"), secret=SecretStr("the-sec"))
        assert cfg.api_key.get_secret_value() == "the-key"


# ---------------------------------------------------------------------------
# ExchangeConfig helpers
# ---------------------------------------------------------------------------

class TestExchangeConfigHelpers:
    def test_is_configured_true(self):
        cfg = ExchangeConfig(api_key=SecretStr("k"), secret=SecretStr("s"))
        assert cfg.is_configured is True

    def test_is_configured_false_no_keys(self):
        cfg = ExchangeConfig()
        assert cfg.is_configured is False

    def test_okx_requires_passphrase(self):
        cfg = OKXConfig(api_key=SecretStr("k"), secret=SecretStr("s"))
        assert cfg.is_configured is False

    def test_okx_configured_with_passphrase(self):
        cfg = OKXConfig(
            api_key=SecretStr("k"),
            secret=SecretStr("s"),
            passphrase=SecretStr("p"),
        )
        assert cfg.is_configured is True
