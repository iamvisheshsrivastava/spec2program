"""Tests for batch mode."""

from __future__ import annotations

import pytest

from backend.batch import run_batch
from backend.config import settings


@pytest.fixture(autouse=True)
def force_mock_provider(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "mock")


def test_batch_runs_every_spec_and_aggregates(simple_spec):
    response = run_batch([simple_spec, simple_spec])

    assert len(response.results) == 2
    assert response.aggregate.vehicles == 2
    assert response.aggregate.valid_count == 2
    assert response.aggregate.validity_rate == 1.0
    assert response.aggregate.avg_cycle_time_seconds > 0
    assert response.aggregate.avg_critical_path_seconds > 0
    assert response.aggregate.bottleneck_ecus  # at least one ECU identified


def test_batch_with_no_specs_returns_empty_aggregate():
    response = run_batch([])
    assert response.results == []
    assert response.aggregate.vehicles == 0
    assert response.aggregate.validity_rate == 0.0
