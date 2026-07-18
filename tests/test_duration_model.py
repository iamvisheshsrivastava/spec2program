"""Tests for the learned step-duration model."""

from __future__ import annotations

import json

from backend import duration_model


def test_predict_returns_none_without_trained_model(tmp_path, monkeypatch):
    monkeypatch.setattr(duration_model, "MODEL_PATH", tmp_path / "does_not_exist.json")
    assert duration_model.is_available() is False
    assert duration_model.predict_seconds("flash_software", 10.0) is None


def test_predict_uses_trained_weights(tmp_path, monkeypatch):
    model_path = tmp_path / "duration_model.json"
    # A trivial hand-written model: bias term (last weight) only, everything
    # else zero, so every step type should predict exactly 7.0 seconds.
    n_features = len(duration_model.STEP_TYPES) + 2  # one-hot + flash term + bias
    weights = [0.0] * (n_features - 1) + [7.0]
    model_path.write_text(json.dumps({"weights": weights}), encoding="utf-8")

    monkeypatch.setattr(duration_model, "MODEL_PATH", model_path)
    assert duration_model.is_available() is True
    assert duration_model.predict_seconds("validation") == 7.0


def test_predict_respects_saved_feature_spec(tmp_path, monkeypatch):
    """A model trained with the mean_baseline candidate (no flash term) must
    ignore flash_size_proxy at inference time, even for flash_software.
    """
    model_path = tmp_path / "duration_model.json"
    feature_spec = {"onehot": True, "flash_linear": False, "flash_quadratic": False, "bias": True}
    n_features = len(duration_model.STEP_TYPES) + 1  # one-hot + bias, no flash term
    weights = [0.0] * (n_features - 1) + [12.0]
    model_path.write_text(
        json.dumps({"weights": weights, "feature_spec": feature_spec}), encoding="utf-8"
    )
    monkeypatch.setattr(duration_model, "MODEL_PATH", model_path)

    # flash_size_proxy is deliberately large; must not move the prediction
    # since this model's feature_spec excludes the flash term entirely.
    assert duration_model.predict_seconds("flash_software", flash_size_proxy=999.0) == 12.0


def test_model_info_surfaces_automl_metadata(tmp_path, monkeypatch):
    model_path = tmp_path / "duration_model.json"
    model_path.write_text(
        json.dumps({
            "model_type": "linear",
            "weights": [0.0] * (len(duration_model.STEP_TYPES) + 2),
            "trained_on_rows": 2400,
            "train_mae_seconds": 1.45,
            "automl": {
                "candidates_evaluated": ["mean_baseline", "linear", "linear_quadratic_flash"],
                "cv_mae_by_candidate": {"mean_baseline": 1.64, "linear": 1.46, "linear_quadratic_flash": 1.46},
                "selected": "linear",
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(duration_model, "MODEL_PATH", model_path)

    info = duration_model.model_info()
    assert info["model_type"] == "linear"
    assert info["automl"]["selected"] == "linear"
    assert "mean_baseline" in info["automl"]["candidates_evaluated"]


def test_model_info_none_without_trained_model(tmp_path, monkeypatch):
    monkeypatch.setattr(duration_model, "MODEL_PATH", tmp_path / "does_not_exist.json")
    assert duration_model.model_info() is None


def test_apply_learned_durations_overrides_estimates(simple_spec, tmp_path, monkeypatch):
    from backend import generator

    model_path = tmp_path / "duration_model.json"
    n_features = len(duration_model.STEP_TYPES) + 2
    weights = [0.0] * (n_features - 1) + [99.0]
    model_path.write_text(json.dumps({"weights": weights}), encoding="utf-8")
    monkeypatch.setattr(duration_model, "MODEL_PATH", model_path)
    monkeypatch.setattr(generator, "duration_model_available", duration_model.is_available)
    monkeypatch.setattr(generator, "predict_seconds", duration_model.predict_seconds)

    from backend.llm_service import MockProvider
    from backend.models import CommissioningProgram

    program = CommissioningProgram.model_validate(
        MockProvider().generate_program(simple_spec)
    )
    applied = generator._apply_learned_durations(simple_spec, program)
    assert applied is True
    assert all(step.estimated_seconds == 99.0 for step in program.steps)
