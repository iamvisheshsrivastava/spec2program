"""Train the step-duration regression model.

Real production telemetry (how long each commissioning step actually took on
the line) is exactly the kind of data this PhD project would have access to
and this demo does not. So this script generates a synthetic but structured
stand-in: for each step type it samples durations around the same base
figures the old hardcoded TIME_BUDGET table used, adds realistic noise, and -
for flash_software steps only - makes duration grow with a payload-size
proxy, mirroring how flashing time scales with software image size in
reality.

It then fits an ordinary-least-squares linear regression (closed form, via
``numpy.linalg.lstsq`` - no extra ML dependency needed) over the same feature
vector ``backend/duration_model.py`` uses at inference time, and writes the
learned weights to ``data/duration_model.json``.

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

from backend.duration_model import STEP_TYPES, features, MODEL_PATH  # noqa: E402

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


def generate_synthetic_log(n_per_type: int = 400, seed: int = 42) -> tuple[np.ndarray, np.ndarray]:
    """Return (X, y): stacked feature rows and observed durations."""
    rng = np.random.default_rng(seed)
    rows: list[list[float]] = []
    targets: list[float] = []

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

            rows.append(features(step_type, flash_size_proxy))
            targets.append(duration)

    return np.array(rows), np.array(targets)


def main() -> None:
    X, y = generate_synthetic_log()
    weights, residuals, rank, _ = np.linalg.lstsq(X, y, rcond=None)

    predictions = X @ weights
    mae = float(np.mean(np.abs(predictions - y)))

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODEL_PATH.write_text(
        json.dumps(
            {
                "weights": weights.tolist(),
                "feature_order": [*STEP_TYPES, "flash_size_term", "bias"],
                "trained_on_rows": int(X.shape[0]),
                "train_mae_seconds": round(mae, 3),
                "note": (
                    "Trained on a synthetic historical-run-log stand-in "
                    "(scripts/train_duration_model.py); replace with real "
                    "telemetry to retrain on actual line data."
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Trained on {X.shape[0]} synthetic rows. Train MAE: {mae:.2f}s.")
    print(f"Weights written to {MODEL_PATH}")


if __name__ == "__main__":
    main()
