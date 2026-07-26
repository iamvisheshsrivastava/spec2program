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


def test_batch_preserves_input_order_when_run_concurrently(simple_spec):
    """Specs are generated on a thread pool, so results must still line up
    with the specs they came from - a rollup that shuffled vehicles would
    silently mis-attribute every per-vehicle metric.
    """
    specs = []
    for i in range(6):
        spec = simple_spec.model_copy(deep=True)
        spec.vehicle_id = f"VEH-{i:03d}"
        specs.append(spec)

    response = run_batch(specs)

    assert [r.program.vehicle_id for r in response.results] == [
        s.vehicle_id for s in specs
    ]
    assert response.aggregate.vehicles == 6


def test_batch_of_one_spec_still_works(simple_spec):
    """The single-spec path skips the thread pool; make sure it behaves."""
    response = run_batch([simple_spec])
    assert len(response.results) == 1
    assert response.aggregate.vehicles == 1


def test_batch_propagates_generation_errors(simple_spec, monkeypatch):
    """A failure inside a worker thread must surface, not be swallowed."""
    from backend import batch

    def _boom(spec):
        raise RuntimeError("generation exploded")

    monkeypatch.setattr(batch, "generate", _boom)
    with pytest.raises(RuntimeError, match="generation exploded"):
        run_batch([simple_spec, simple_spec])
