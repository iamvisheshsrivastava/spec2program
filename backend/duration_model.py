"""Learned step-duration model.

The mock planner's ``TIME_BUDGET`` table (see ``llm_service.py``) is a fixed,
hand-guessed number per step type. That is a reasonable starting point but it
is exactly the kind of thing the PhD project's task list calls out: "analyse
and derive data-driven improvements." This module replaces the fixed table
with a small regression model trained on run-log data (here, a synthetic
dataset standing in for real production telemetry - see
``scripts/train_duration_model.py``), so per-step time estimates come from
data rather than a guess, and can be retrained as real logs accumulate.

The model itself is intentionally simple - ordinary least squares over a
handful of interpretable features - because interpretability matters more
than raw accuracy for a system whose numbers feed process-engineering
decisions. It is not meant to be state-of-the-art AutoML; it is meant to
demonstrate the *shape* of a data-driven estimate replacing a hardcoded one.
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


def features(step_type: str, flash_size_proxy: float) -> list[float]:
    """Build the feature vector for one step.

    Features: a one-hot encoding of step type, a flash-size proxy (only
    meaningful for flash_software steps - the length of the target software
    version string stands in for payload size, since we have no real binary
    sizes), and a bias term.
    """
    onehot = [1.0 if step_type == t else 0.0 for t in STEP_TYPES]
    flash_term = flash_size_proxy if step_type == "flash_software" else 0.0
    return [*onehot, flash_term, 1.0]


def is_available() -> bool:
    """Whether a trained model file exists on disk."""
    return MODEL_PATH.exists()


def _load_weights() -> list[float] | None:
    if not MODEL_PATH.exists():
        return None
    return json.loads(MODEL_PATH.read_text(encoding="utf-8"))["weights"]


def predict_seconds(step_type: str, flash_size_proxy: float = 0.0) -> float | None:
    """Predict a step's duration in seconds, or None if no model is trained."""
    weights = _load_weights()
    if weights is None:
        return None
    x = np.array(features(step_type, flash_size_proxy))
    w = np.array(weights)
    predicted = float(np.dot(w, x))
    # Durations can't be negative or implausibly tiny; floor it.
    return max(0.5, predicted)
