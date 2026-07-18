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
