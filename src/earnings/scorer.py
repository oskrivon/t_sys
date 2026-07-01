"""LLM transcript scorer for earnings calls (Phase 2)."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Optional

import structlog

from .config import TRANSCRIPT_PROMPT
from .models import EarningsEvent, TranscriptScore

log = structlog.get_logger()


class TranscriptScorer:
    """Score earnings transcripts using Claude via OpenRouter."""

    def __init__(
        self,
        api_key: str,
        model: str = "anthropic/claude-sonnet-4-20250514",
        temperature: float = 0.0,
    ):
        self.api_key = api_key
        self.model = model
        self.temperature = temperature

    async def score(
        self,
        event: EarningsEvent,
        transcript: str,
        company_name: str = "",
        quarter_label: str = "",
    ) -> Optional[tuple[TranscriptScore, str, str]]:
        """Score a transcript.

        Returns:
            (TranscriptScore, prompt_hash, raw_response) or None on failure.
        """
        import openai

        if not transcript or not transcript.strip():
            return None

        # Build prompt
        prompt = TRANSCRIPT_PROMPT.format(
            symbol=event.symbol,
            company_name=company_name or event.symbol,
            quarter=quarter_label or "Unknown",
            eps_surprise_pct=event.eps_surprise_pct or 0,
            eps_actual=event.eps_actual or 0,
            eps_estimate=event.eps_estimate or 0,
            transcript_text=transcript,
        )

        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]

        try:
            client = openai.AsyncOpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=self.api_key,
            )

            response = await client.chat.completions.create(
                model=self.model,
                max_tokens=500,
                temperature=self.temperature,
                messages=[{"role": "user", "content": prompt}],
            )

            text = response.choices[0].message.content.strip()

            # Parse JSON (handle markdown-wrapped)
            json_text = text
            if json_text.startswith("```"):
                json_text = json_text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

            try:
                data = json.loads(json_text)
                score = TranscriptScore(**data)
                return score, prompt_hash, text
            except (json.JSONDecodeError, Exception) as e:
                # Fallback: try to extract composite_score
                m = re.search(r'"composite_score"\s*:\s*([+-]?\d+\.?\d*)', text)
                if m:
                    score = TranscriptScore(
                        sentiment=0,
                        guidance_direction=0,
                        management_confidence=3,
                        composite_score=float(m.group(1)),
                        reasoning=text[:200],
                    )
                    return score, prompt_hash, text

                log.warning(
                    "transcript_parse_error",
                    symbol=event.symbol,
                    error=str(e),
                    text=text[:200],
                )
                return None

        except Exception as e:
            log.error(
                "transcript_api_error",
                symbol=event.symbol,
                error=str(e),
            )
            return None
