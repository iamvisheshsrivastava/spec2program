"""Learned step-duration model.

The mock planner's ``TIME_BUDGET`` table (see ``llm_service.py``) is a fixed,
hand-guessed number per step type. That is a reasonable starting point but it
is exactly the kind of thing the PhD project's task list calls out: "analyse
and derive data-driven improvements." This module replaces the fixed table
with a small regression model trained on run-log data (here, a synthetic
dataset standing in for real production telemetry - see
``scripts/train_duration_model.py``), so per-step time estimates come from
data rather than a guess, and can be retrained as real logs accumulate.

``scripts/train_duration_model.py`` performs a small, honest AutoML step: it
fits several candidate feature sets (a per-step-type mean baseline, a linear
flash-size term, a quadratic flash-size term), scores each with k-fold
cross-validation, and keeps whichever generalises best. This module only
needs to know how to rebuild whatever feature vector the *winning* model was
trained on - stored alongside the weights as ``feature_spec`` - so inference
stays correct regardless of which candidate won.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

MODEL_PATH = Path(__file__).resolve().parent.parent / "data" / "duration_model.json"

# Must match the step_type values in StepType (models.py).
STEP_TYPES = [
    "diagnostic_session",
    "security_access",
    "flash_software",
    "write_parameter",
    "validation",
    "fault_clear",
]

# The feature set used by any model file saved before AutoML selection was
# introduced (or by a hand-written test fixture that omits "feature_spec").
# Keeping this as the default preserves backward compatibility.
DEFAULT_FEATURE_SPEC = {
    "onehot": True,
    "flash_linear": True,
    "flash_quadratic": False,
    "bias": True,
}


def build_features(step_type: str, flash_size_proxy: float, feature_spec: dict) -> list[float]:
    """Build a feature vector for one step, per an explicit feature spec.

    ``feature_spec`` toggles which feature groups are present, so the same
    function can reconstruct the exact input a model was trained on,
    whichever of the AutoML candidates it turned out to be:
      - "onehot": one-hot encoding of step type.
      - "flash_linear": flash-size proxy (only nonzero for flash_software).
      - "flash_quadratic": squared flash-size proxy (captures the idea that
        very large flash payloads take disproportionately longer).
      - "bias": constant 1.0 term.
    """
    feats: list[float] = []
    if feature_spec.get("onehot", True):
        feats.extend(1.0 if step_type == t else 0.0 for t in STEP_TYPES)

    is_flash = 1.0 if step_type == "flash_software" else 0.0
    if feature_spec.get("flash_linear", True):
        feats.append(is_flash * flash_size_proxy)
    if feature_spec.get("flash_quadratic", False):
        feats.append(is_flash * (flash_size_proxy ** 2))
    if feature_spec.get("bias", True):
        feats.append(1.0)
    return feats


def features(step_type: str, flash_size_proxy: float) -> list[float]:
    """Backward-compatible feature builder using the default feature spec."""
    return build_features(step_type, flash_size_proxy, DEFAULT_FEATURE_SPEC)


def is_available() -> bool:
    """Whether a trained model file exists on disk."""
    return MODEL_PATH.exists()


def _load_model() -> dict | None:
    if not MODEL_PATH.exists():
        return None
    return json.loads(MODEL_PATH.read_text(encoding="utf-8"))


def predict_seconds(step_type: str, flash_size_proxy: float = 0.0) -> float | None:
    """Predict a step's duration in seconds, or None if no model is trained."""
    model = _load_model()
    if model is None:
        return None
    weights = model["weights"]
    feature_spec = model.get("feature_spec", DEFAULT_FEATURE_SPEC)
    x = np.array(build_features(step_type, flash_size_proxy, feature_spec))
    w = np.array(weights)
    predicted = float(np.dot(w, x))
    # Durations can't be negative or implausibly tiny; floor it.
    return max(0.5, predicted)


def model_info() -> dict | None:
    """Return the trained model's metadata (type, AutoML results), if any."""
    model = _load_model()
    if model is None:
        return None
    return {
        "model_type": model.get("model_type", "linear (legacy, pre-AutoML)"),
        "automl": model.get("automl"),
        "trained_on_rows": model.get("trained_on_rows"),
        "train_mae_seconds": model.get("train_mae_seconds"),
    }
