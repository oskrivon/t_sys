"""ML scorer — model registry, drift monitoring, prediction quality tracking.

Loads versioned GradientBoosting models, predicts P(win), monitors feature
drift against training distribution, and tracks prediction accuracy over time.
"""
from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import structlog

log = structlog.get_logger()

# Feature order must match training.
# New features (regime_*) are appended — old models ignore them
# because they default to 0.0 and aren't in the old feature set.
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
    # Regime features (v2+)
    "regime_adx", "regime_efficiency", "regime_vol_ratio",
]


# ---------------------------------------------------------------------------
# Model metadata
# ---------------------------------------------------------------------------

@dataclass
class ModelMeta:
    """Metadata stored alongside each model version."""
    version: int
    training_date: str = ""
    feature_names: list[str] = field(default_factory=list)
    n_train_trades: int = 0
    train_win_rate: float = 0.0
    train_sharpe: float = 0.0
    # Feature distribution from training set (for drift detection)
    feature_means: dict[str, float] = field(default_factory=dict)
    feature_stds: dict[str, float] = field(default_factory=dict)

    def save(self, path: Path) -> None:
        with open(path, "w") as f:
            json.dump(self.__dict__, f, indent=2)

    @classmethod
    def load(cls, path: Path) -> ModelMeta:
        with open(path) as f:
            data = json.load(f)
        return cls(**data)


# ---------------------------------------------------------------------------
# Drift monitor
# ---------------------------------------------------------------------------

class DriftMonitor:
    """Tracks feature drift vs training distribution.

    Uses a simple z-score approach: if the rolling mean of a feature
    deviates by more than ``threshold`` standard deviations from the
    training mean, it's flagged as drifted.
    """

    def __init__(
        self,
        training_means: dict[str, float],
        training_stds: dict[str, float],
        buffer_size: int = 200,
        z_threshold: float = 2.5,
    ):
        self._train_means = training_means
        self._train_stds = training_stds
        self._z_threshold = z_threshold
        self._buffer: deque[dict[str, float]] = deque(maxlen=buffer_size)
        self._last_report: list[str] = []

    def record(self, features: dict[str, float]) -> None:
        """Add a feature vector to the rolling buffer."""
        self._buffer.append(features)

    def check_drift(self) -> tuple[float, list[str]]:
        """Check for drift. Returns (drift_score, list of drifted feature names).

        drift_score: fraction of features that are drifted (0.0 - 1.0).
        """
        if len(self._buffer) < 30 or not self._train_means:
            return 0.0, []

        drifted = []
        for name in self._train_means:
            train_mu = self._train_means[name]
            train_std = self._train_stds.get(name, 1.0)
            if train_std <= 0:
                continue

            values = [b.get(name, 0.0) for b in self._buffer]
            current_mu = float(np.mean(values))
            z = abs(current_mu - train_mu) / train_std
            if z > self._z_threshold:
                drifted.append(name)

        n_features = len(self._train_means)
        score = len(drifted) / n_features if n_features > 0 else 0.0

        if drifted and drifted != self._last_report:
            log.warning("feature_drift_detected",
                        drifted_features=drifted,
                        drift_score=f"{score:.2f}",
                        buffer_size=len(self._buffer))
            self._last_report = drifted

        return score, drifted


# ---------------------------------------------------------------------------
# Prediction quality tracker
# ---------------------------------------------------------------------------

class PredictionTracker:
    """Tracks prediction accuracy via Brier score and rolling accuracy."""

    def __init__(self, window: int = 100):
        self._window = window
        self._history: deque[tuple[float, int]] = deque(maxlen=window)

    def record_outcome(self, predicted_proba: float, actual_win: bool) -> None:
        """Record a trade outcome for quality tracking."""
        self._history.append((predicted_proba, int(actual_win)))

    @property
    def brier_score(self) -> float:
        """Brier score (lower is better, 0 = perfect, 0.25 = random)."""
        if not self._history:
            return 0.25
        return float(np.mean([
            (proba - actual) ** 2 for proba, actual in self._history
        ]))

    @property
    def rolling_accuracy(self) -> float:
        """Fraction of predictions where direction was correct.

        Correct = (proba > 0.5 AND win) OR (proba <= 0.5 AND loss).
        """
        if not self._history:
            return 0.5
        correct = sum(
            1 for proba, actual in self._history
            if (proba > 0.5) == bool(actual)
        )
        return correct / len(self._history)

    @property
    def n_outcomes(self) -> int:
        return len(self._history)

    def get_stats(self) -> dict:
        return {
            "brier_score": self.brier_score,
            "rolling_accuracy": self.rolling_accuracy,
            "n_outcomes": self.n_outcomes,
        }


# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------

class ModelRegistry:
    """Manages versioned models in a directory.

    Expects files named ``{prefix}_v{N}.joblib`` with optional
    ``{prefix}_v{N}.meta.json`` sidecars.
    """

    def __init__(self, model_dir: str = "data/models", prefix: str = "miro_gb"):
        self.model_dir = Path(model_dir)
        self.prefix = prefix
        self._pattern = re.compile(rf"^{re.escape(prefix)}_v(\d+)\.joblib$")

    def list_versions(self) -> list[int]:
        """List available model versions, sorted ascending."""
        if not self.model_dir.exists():
            return []
        versions = []
        for p in self.model_dir.iterdir():
            m = self._pattern.match(p.name)
            if m:
                versions.append(int(m.group(1)))
        return sorted(versions)

    def latest_version(self) -> Optional[int]:
        versions = self.list_versions()
        return versions[-1] if versions else None

    def model_path(self, version: int) -> Path:
        return self.model_dir / f"{self.prefix}_v{version}.joblib"

    def meta_path(self, version: int) -> Path:
        return self.model_dir / f"{self.prefix}_v{version}.meta.json"

    def load_meta(self, version: int) -> Optional[ModelMeta]:
        path = self.meta_path(version)
        if path.exists():
            try:
                return ModelMeta.load(path)
            except Exception:
                log.warning("meta_load_failed", version=version)
        return None


# ---------------------------------------------------------------------------
# ML Scorer (main class)
# ---------------------------------------------------------------------------

class MLScorer:
    """Loads a versioned model and predicts win probability.

    Includes drift monitoring and prediction quality tracking.
    """

    def __init__(self, model_path: str = "data/models/miro_gb_v1.joblib"):
        self.model_path = Path(model_path)
        self._model = None
        self._version: Optional[int] = None
        self._meta: Optional[ModelMeta] = None
        self._feature_names: list[str] = []
        self._registry = ModelRegistry(
            model_dir=str(self.model_path.parent),
        )
        self._drift_monitor: Optional[DriftMonitor] = None
        self._prediction_tracker = PredictionTracker()

    def load(self) -> bool:
        """Load model from disk. Returns True if successful."""
        if not self.model_path.exists():
            log.warning("ml_model_not_found", path=str(self.model_path))
            return False
        try:
            import joblib
            self._model = joblib.load(self.model_path)
            self._version = self._detect_version()
            self._meta = self._load_meta()
            self._feature_names = (
                self._meta.feature_names if self._meta and self._meta.feature_names
                else list(FEATURE_NAMES)
            )
            # Initialize drift monitor if training stats available
            if self._meta and self._meta.feature_means:
                self._drift_monitor = DriftMonitor(
                    training_means=self._meta.feature_means,
                    training_stds=self._meta.feature_stds,
                )
            log.info("ml_model_loaded", path=str(self.model_path),
                     version=self._version,
                     n_features=len(self._feature_names))
            return True
        except Exception as e:
            log.error("ml_model_load_error", error=str(e))
            return False

    def load_latest(self) -> bool:
        """Load the latest available model version."""
        v = self._registry.latest_version()
        if v is None:
            log.warning("no_models_found")
            return False
        self.model_path = self._registry.model_path(v)
        return self.load()

    def load_version(self, version: int) -> bool:
        """Load a specific model version."""
        self.model_path = self._registry.model_path(version)
        return self.load()

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def version(self) -> Optional[int]:
        return self._version

    def predict(self, features: dict[str, float]) -> Optional[float]:
        """Predict P(win) for a signal.

        Args:
            features: Dict from compute_features().

        Returns:
            Probability of win (0-1), or None if model not loaded.
        """
        if self._model is None:
            return None

        # Build feature vector using the model's feature list
        x = np.array([[features.get(name, 0.0) for name in self._feature_names]])

        try:
            proba = self._model.predict_proba(x)[0, 1]

            # Track drift
            if self._drift_monitor:
                self._drift_monitor.record(features)

            return float(proba)
        except Exception as e:
            log.error("ml_predict_error", error=str(e))
            return None

    def record_outcome(self, predicted_proba: float, actual_win: bool) -> None:
        """Record a trade outcome for quality tracking."""
        self._prediction_tracker.record_outcome(predicted_proba, actual_win)

        if self._prediction_tracker.n_outcomes % 50 == 0:
            stats = self._prediction_tracker.get_stats()
            log.info("ml_prediction_quality", **stats)
            if stats["brier_score"] > 0.3:
                log.warning("ml_calibration_poor",
                            brier=stats["brier_score"],
                            msg="Model predictions are poorly calibrated — consider retraining")

    def check_drift(self) -> tuple[float, list[str]]:
        """Check feature drift. Returns (score, drifted_features)."""
        if self._drift_monitor:
            return self._drift_monitor.check_drift()
        return 0.0, []

    def get_status(self) -> dict:
        """Full scorer status."""
        drift_score, drifted = self.check_drift()
        return {
            "loaded": self.is_loaded,
            "version": self._version,
            "model_path": str(self.model_path),
            "n_features": len(self._feature_names),
            "drift_score": drift_score,
            "drifted_features": drifted,
            "prediction_quality": self._prediction_tracker.get_stats(),
            "available_versions": self._registry.list_versions(),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _detect_version(self) -> Optional[int]:
        """Extract version number from model filename."""
        m = re.search(r"_v(\d+)\.joblib$", self.model_path.name)
        return int(m.group(1)) if m else None

    def _load_meta(self) -> Optional[ModelMeta]:
        """Load metadata sidecar for current model."""
        if self._version is not None:
            return self._registry.load_meta(self._version)
        return None
