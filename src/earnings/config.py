"""Earnings backtest configuration."""
from __future__ import annotations

from pydantic import BaseModel, field_validator


class EarningsConfig(BaseModel):
    """Configuration for earnings PEAD backtest."""

    # Universe & period
    universe: str = "sp100"  # "sp100" or "sp500"
    start_date: str = "2022-01-01"
    end_date: str = "2025-12-31"

    # Backtest parameters
    hold_periods: list[int] = [1, 3, 5, 10]
    position_size_usd: float = 10_000.0
    surprise_threshold_pct: float = 5.0  # |surprise| > X% to trade

    # Data sources
    fmp_api_key: str = ""
    fmp_calls_per_day: int = 250

    # LLM (Phase 2)
    llm_model: str = "anthropic/claude-sonnet-4-20250514"
    openrouter_api_key: str = ""
    max_transcript_tokens: int = 12_000
    scorer_temperature: float = 0.0

    # Storage
    db_path: str = "data/earnings_backtest.db"
    cache_dir: str = "data/cache/earnings"

    @field_validator("universe")
    @classmethod
    def valid_universe(cls, v: str) -> str:
        if v not in ("sp100", "sp500"):
            raise ValueError(f"universe must be sp100 or sp500, got {v}")
        return v

    @field_validator("surprise_threshold_pct")
    @classmethod
    def valid_threshold(cls, v: float) -> float:
        if v < 0:
            raise ValueError("surprise_threshold_pct must be >= 0")
        return v


# ── Prompt template for Phase 2 LLM scoring ──────────────────────────

TRANSCRIPT_PROMPT = """You are a senior equity analyst evaluating an earnings call transcript.

COMPANY: {symbol} ({company_name})
QUARTER: {quarter}
EPS SURPRISE: {eps_surprise_pct:+.1f}% (actual: {eps_actual:.2f}, estimate: {eps_estimate:.2f})

TRANSCRIPT:
{transcript_text}

Analyze this transcript and predict the stock's post-earnings price drift \
over the next 1-10 trading days. Focus on:

1. MANAGEMENT TONE: Confident vs defensive? Forward-looking vs backward-looking?
2. GUIDANCE: Was forward guidance raised, maintained, or lowered? Any sandbagging signals?
3. QUALITY OF BEAT/MISS: Was the EPS surprise driven by operations or one-time items?
4. KEY RISKS: What specific risks or headwinds were mentioned or dodged?
5. ANALYST SENTIMENT: Were analyst questions hostile, constructive, or softball?

Respond with ONLY this JSON:
{{
  "sentiment": <int -5 to +5>,
  "guidance_direction": <int -2 to +2>,
  "management_confidence": <int 1 to 5>,
  "risk_flags": [<list of short risk labels>],
  "key_themes": [<list of 2-4 key themes>],
  "composite_score": <float -10 to +10>,
  "reasoning": "<2-3 sentences explaining your assessment>"
}}"""
