"""Claude Vision scorer — evaluates chart setup quality via OpenRouter API."""
from __future__ import annotations

import base64
import json
import re
from typing import Optional

import structlog

log = structlog.get_logger()

PROMPT_TEMPLATE = """You are an expert crypto trader evaluating chart setups.

This is a {tf} candlestick chart for {symbol}.
- Green horizontal lines = support levels
- Red horizontal lines = resistance levels
- The blue vertical line marks where a {signal_type} signal was detected
- Arrow shows the entry direction ({direction})

Evaluate this setup quality on a scale 1-10:
- 1-3: Bad setup (messy levels, no clear structure, against trend)
- 4-5: Mediocre (level exists but context is weak)
- 6-7: Good (clear level, clean retouches, aligned with structure)
- 8-10: Excellent (textbook pattern, strong level, perfect context)

Respond ONLY with JSON: {{"score": N, "reason": "brief explanation in 1-2 sentences"}}"""


class VisionScorer:
    """Evaluates trade setup quality using Claude Vision via OpenRouter."""

    def __init__(self, api_key: str, model: str = "anthropic/claude-sonnet-4.6"):
        self.api_key = api_key
        self.model = model

    async def score(
        self,
        chart_png: bytes,
        symbol: str,
        signal_type: str,
        is_long: bool,
        timeframe: str = "4h",
    ) -> Optional[dict]:
        """Score a chart setup asynchronously.

        Args:
            chart_png: PNG image bytes.
            symbol: Trading pair (e.g., "SOL/USDT").
            signal_type: Signal type string.
            is_long: Trade direction.
            timeframe: Chart timeframe.

        Returns:
            {"score": int, "reason": str} or None on failure.
        """
        import openai

        direction = "LONG (buy)" if is_long else "SHORT (sell)"
        prompt = PROMPT_TEMPLATE.format(
            tf=timeframe, symbol=symbol,
            signal_type=signal_type, direction=direction,
        )

        image_b64 = base64.b64encode(chart_png).decode()

        try:
            client = openai.AsyncOpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=self.api_key,
            )

            response = await client.chat.completions.create(
                model=self.model,
                max_tokens=200,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/png;base64,{image_b64}",
                        }},
                        {"type": "text", "text": prompt},
                    ],
                }],
            )

            text = response.choices[0].message.content.strip()

            # Parse JSON (handle markdown-wrapped)
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

            try:
                result = json.loads(text)
                return result
            except json.JSONDecodeError:
                m = re.search(r'"score"\s*:\s*(\d+)', text)
                if m:
                    return {"score": int(m.group(1)), "reason": text[:200]}
                log.warning("vision_parse_error", text=text[:200])
                return None

        except Exception as e:
            log.error("vision_api_error", error=str(e))
            return None
