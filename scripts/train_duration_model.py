"""Train the step-duration regression model, with automatic model selection.

Real production telemetry (how long each commissioning step actually took on
the line) is exactly the kind of data this PhD project would have access to
and this demo does not. So this script generates a synthetic but structured
stand-in: for each step type it samples durations around the same base
figures the old hardcoded TIME_BUDGET table used, adds realistic noise, and -
for flash_software steps only - makes duration grow with a payload-size
proxy, mirroring how flashing time scales with software image size in
reality.

This is also where the small, honest "AutoML" step lives: rather than
committing to one feature set by hand, the script fits three candidate
models -

  1. mean_baseline          - a plain per-step-type average (no flash term).
  2. linear                 - + a linear flash-size term.
  3. linear_quadratic_flash - + a quadratic flash-size term.

- scores each with 5-fold cross-validation (closed-form OLS per fold via
``numpy.linalg.lstsq``, no extra ML dependency needed), and keeps whichever
generalises best (lowest mean CV MAE). This is deliberately small in scope:
it is automated model *selection* over a handful of interpretable candidates,
not neural-architecture search - but it is genuinely automated, and the CV
scores for every candidate are written to disk for transparency rather than
just the winner's.

Usage:
    python scripts/train_duration_model.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.duration_model import STEP_TYPES, build_features, MODEL_PATH  # noqa: E402

# Base duration (seconds) and noise std-dev per step type - stand-in for what
# would, in reality, be summary statistics pulled from historical run logs.
BASE_SECONDS = {
    "diagnostic_session": 3.0,
    "security_access": 4.0,
    "flash_software": 30.0,  # base; grows with the flash-size proxy below
    "write_parameter": 6.0,
    "validation": 8.0,
    "fault_clear": 5.0,
}
NOISE_STD = {
    "diagnostic_session": 0.6,
    "security_access": 0.8,
    "flash_software": 6.0,
    "write_parameter": 1.2,
    "validation": 1.5,
    "fault_clear": 1.0,
}
# Extra seconds per unit of the flash-size proxy (only applies to flashing).
FLASH_SIZE_SLOPE = 0.9

# The AutoML candidate pool: each is a feature_spec understood by
# backend.duration_model.build_features().
CANDIDATES: dict[str, dict] = {
    "mean_baseline": {
        "onehot": True, "flash_linear": False, "flash_quadratic": False, "bias": True,
    },
    "linear": {
        "onehot": True, "flash_linear": True, "flash_quadratic": False, "bias": True,
    },
    "linear_quadratic_flash": {
        "onehot": True, "flash_linear": True, "flash_quadratic": True, "bias": True,
    },
}


def generate_synthetic_log(n_per_type: int = 400, seed: int = 42) -> list[tuple[str, float, float]]:
    """Return raw (step_type, flash_size_proxy, observed_duration) rows."""
    rng = np.random.default_rng(seed)
    rows: list[tuple[str, float, float]] = []

    for step_type in STEP_TYPES:
        for _ in range(n_per_type):
            flash_size_proxy = 0.0
            base = BASE_SECONDS[step_type]
            if step_type == "flash_software":
                # Proxy for image size, in the same units as len(version_string)
                # used at inference time (roughly 8-24 characters).
                flash_size_proxy = rng.uniform(8, 24)
                base = base + FLASH_SIZE_SLOPE * flash_size_proxy
            noise = rng.normal(0, NOISE_STD[step_type])
            duration = max(0.2, base + noise)
            rows.append((step_type, flash_size_proxy, duration))

    return rows


def _fit(rows: list[tuple[str, float, float]], indices, feature_spec: dict):
    X = np.array([build_features(rows[i][0], rows[i][1], feature_spec) for i in indices])
    y = np.array([rows[i][2] for i in indices])
    weights, *_ = np.linalg.lstsq(X, y, rcond=None)
    return weights, X, y


def kfold_cv_mae(rows: list[tuple[str, float, float]], feature_spec: dict, k: int = 5, seed: int = 0) -> float:
    """Mean absolute error, averaged over k held-out folds."""
    rng = np.random.default_rng(seed)
    order = rng.permutation(len(rows))
    folds = np.array_split(order, k)

    fold_maes = []
    for i in range(k):
        test_idx = folds[i]
        train_idx = np.concatenate([folds[j] for j in range(k) if j != i])
        weights, _, _ = _fit(rows, train_idx, feature_spec)
        X_test = np.array([build_features(rows[t][0], rows[t][1], feature_spec) for t in test_idx])
        y_test = np.array([rows[t][2] for t in test_idx])
        pred = X_test @ weights
        fold_maes.append(float(np.mean(np.abs(pred - y_test))))
    return float(np.mean(fold_maes))


def main() -> None:
    rows = generate_synthetic_log()
    all_idx = np.arange(len(rows))

    # --- AutoML: score every candidate feature set via cross-validation. ---
    cv_scores: dict[str, float] = {
        name: kfold_cv_mae(rows, spec) for name, spec in CANDIDATES.items()
    }
    best_name = min(cv_scores, key=cv_scores.get)
    best_spec = CANDIDATES[best_name]

    print("AutoML candidate scores (5-fold CV mean absolute error, seconds):")
    for name, score in sorted(cv_scores.items(), key=lambda kv: kv[1]):
        marker = "  <- selected" if name == best_name else ""
        print(f"  {name:24s} {score:6.3f}{marker}")

    # --- Refit the winning candidate on the full dataset for deployment. ---
    weights, X_full, y_full = _fit(rows, all_idx, best_spec)
    train_mae = float(np.mean(np.abs(X_full @ weights - y_full)))

    feature_order: list[str] = []
    if best_spec.get("onehot", True):
        feature_order.extend(STEP_TYPES)
    if best_spec.get("flash_linear", True):
        feature_order.append("flash_size_linear")
    if best_spec.get("flash_quadratic", False):
        feature_order.append("flash_size_quadratic")
    if best_spec.get("bias", True):
        feature_order.append("bias")

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODEL_PATH.write_text(
        json.dumps(
            {
                "model_type": best_name,
                "feature_spec": best_spec,
                "weights": weights.tolist(),
                "feature_order": feature_order,
                "trained_on_rows": len(rows),
                "train_mae_seconds": round(train_mae, 3),
                "automl": {
                    "candidates_evaluated": list(CANDIDATES.keys()),
                    "cv_mae_by_candidate": {k: round(v, 3) for k, v in cv_scores.items()},
                    "selection_method": "5-fold cross-validation, lowest mean MAE wins",
                    "selected": best_name,
                },
                "note": (
                    "Trained on a synthetic historical-run-log stand-in "
                    "(scripts/train_duration_model.py); replace with real "
                    "telemetry to retrain on actual line data. Model family "
                    "chosen automatically via cross-validation over a small "
                    "candidate pool (see 'automl')."
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nSelected model: {best_name} (train MAE: {train_mae:.2f}s on {len(rows)} rows)")
    print(f"Weights written to {MODEL_PATH}")


if __name__ == "__main__":
    main()
