"""ML scorer — loads pre-trained GradientBoosting model, predicts P(win)."""
from __future__ import annotations

import structlog
from pathlib import Path
from typing import Optional

log = structlog.get_logger()

# Feature order must match training
FEATURE_NAMES = [
    "level_touches", "level_score", "level_age", "is_round_number",
    "zone_width_pct", "distance_to_level_pct",
    "is_retest", "is_zakol", "is_long",
    "body_ratio", "upper_wick_ratio", "lower_wick_ratio", "is_green",
    "distance_to_sma50_pct", "distance_to_sma20_pct",
    "green_candles_10", "trend_strength_10",
    "volatility_20", "atr_20", "atr_vs_zone",
    "volume_ratio", "volume_trend",
    "price_change_7d", "price_change_30d", "abs_move_7d", "abs_move_30d",
    "rsi_14",
    "hour_utc", "day_of_week",
]


class MLScorer:
    """Loads a pre-trained model and predicts win probability."""

    def __init__(self, model_path: str = "data/models/miro_gb_v1.joblib"):
        self.model_path = Path(model_path)
        self._model = None

    def load(self) -> bool:
        """Load model from disk. Returns True if successful."""
        if not self.model_path.exists():
            log.warning("ml_model_not_found", path=str(self.model_path))
            return False
        try:
            import joblib
            self._model = joblib.load(self.model_path)
            log.info("ml_model_loaded", path=str(self.model_path))
            return True
        except Exception as e:
            log.error("ml_model_load_error", error=str(e))
            return False

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def predict(self, features: dict[str, float]) -> Optional[float]:
        """Predict P(win) for a signal.

        Args:
            features: Dict from compute_features().

        Returns:
            Probability of win (0-1), or None if model not loaded.
        """
        if self._model is None:
            return None

        import numpy as np
        x = np.array([[features.get(name, 0.0) for name in FEATURE_NAMES]])
        try:
            proba = self._model.predict_proba(x)[0, 1]
            return float(proba)
        except Exception as e:
            log.error("ml_predict_error", error=str(e))
            return None
