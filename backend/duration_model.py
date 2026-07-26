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
import threading
from functools import lru_cache
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


# ---------------------------------------------------------------------------
# Model cache
# ---------------------------------------------------------------------------
# The model file is small but ``predict_seconds()`` is called once per step,
# for every step of every program, in every request. Re-reading and re-parsing
# the JSON each time made the generation pipeline disk-bound for no reason.
# We cache the parsed model (plus its weights as a ready-made numpy array) and
# invalidate on the file's mtime/size, so retraining is still picked up
# without a restart.
_cache_lock = threading.Lock()
_cached_stamp: tuple[str, float, int] | None = None
_cached_model: dict | None = None


def _file_stamp() -> tuple[str, float, int] | None:
    """(path, mtime, size) of the model file, or None if it does not exist.

    The path is part of the stamp because ``MODEL_PATH`` is a module global
    that callers (and tests) may repoint at a different file; keying on
    mtime/size alone could otherwise serve a stale model for a same-sized
    file written within the same filesystem timestamp tick.
    """
    try:
        st = MODEL_PATH.stat()
    except OSError:
        return None
    return (str(MODEL_PATH), st.st_mtime, st.st_size)


def _load_model() -> dict | None:
    """Return the parsed model, reading from disk only when it has changed."""
    global _cached_stamp, _cached_model

    stamp = _file_stamp()
    if stamp is None:
        with _cache_lock:
            _cached_stamp, _cached_model = None, None
        _predict_cached.cache_clear()
        return None

    # Fast path: the file is unchanged since we last parsed it.
    if stamp == _cached_stamp and _cached_model is not None:
        return _cached_model

    with _cache_lock:
        # Re-check inside the lock; another thread may have just loaded it.
        if stamp == _cached_stamp and _cached_model is not None:
            return _cached_model

        model = json.loads(MODEL_PATH.read_text(encoding="utf-8"))
        # Pre-build the weight vector once instead of per prediction.
        model["_weights_array"] = np.asarray(model["weights"], dtype=float)
        _cached_model = model
        _cached_stamp = stamp

    # The weights changed, so any memoised predictions are now stale.
    _predict_cached.cache_clear()
    return _cached_model


def is_available() -> bool:
    """Whether a trained model file exists on disk."""
    return _file_stamp() is not None


@lru_cache(maxsize=512)
def _predict_cached(step_type: str, flash_size_proxy: float) -> float:
    """Memoised prediction for one (step_type, flash_size) pair.

    A program repeats the same handful of step types many times over, so the
    same feature vector is scored again and again. Cleared whenever the model
    file changes (see ``_load_model``).
    """
    model = _cached_model
    feature_spec = model.get("feature_spec", DEFAULT_FEATURE_SPEC)
    x = np.asarray(build_features(step_type, flash_size_proxy, feature_spec), dtype=float)
    predicted = float(np.dot(model["_weights_array"], x))
    # Durations can't be negative or implausibly tiny; floor it.
    return max(0.5, predicted)


def predict_seconds(step_type: str, flash_size_proxy: float = 0.0) -> float | None:
    """Predict a step's duration in seconds, or None if no model is trained."""
    if _load_model() is None:
        return None
    return _predict_cached(step_type, flash_size_proxy)


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
