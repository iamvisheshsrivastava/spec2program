"""Tests for the critical-path scheduler."""

from __future__ import annotations

from backend.generator import generate
from backend.models import CommissioningProgram, CommissioningStep, StepType
from backend.scheduler import compute_optimization


def test_empty_program_has_zero_optimization():
    program = CommissioningProgram(vehicle_id="V1", steps=[])
    result = compute_optimization(program)
    assert result.sequential_seconds == 0.0
    assert result.critical_path_seconds == 0.0
    assert result.speedup_factor == 1.0
    assert result.schedule == []


def test_independent_steps_run_in_parallel():
    """Two steps with no dependency on each other should overlap in the schedule."""
    program = CommissioningProgram(
        vehicle_id="V1",
        steps=[
            CommissioningStep(order=1, step_type=StepType.DIAGNOSTIC_SESSION,
                               ecu_id="A", description="a", estimated_seconds=10, depends_on=[]),
            CommissioningStep(order=2, step_type=StepType.DIAGNOSTIC_SESSION,
                               ecu_id="B", description="b", estimated_seconds=10, depends_on=[]),
        ],
    )
    result = compute_optimization(program)
    assert result.sequential_seconds == 20.0
    # Both are independent -> critical path is just the longer one (10s), not 20s.
    assert result.critical_path_seconds == 10.0
    assert result.speedup_factor == 2.0


def test_dependent_chain_cannot_be_parallelised():
    """A strict A -> B -> C dependency chain must equal the sequential total."""
    program = CommissioningProgram(
        vehicle_id="V1",
        steps=[
            CommissioningStep(order=1, step_type=StepType.DIAGNOSTIC_SESSION,
                               ecu_id="A", description="a", estimated_seconds=5, depends_on=[]),
            CommissioningStep(order=2, step_type=StepType.SECURITY_ACCESS,
                               ecu_id="A", description="b", estimated_seconds=5, depends_on=[1]),
            CommissioningStep(order=3, step_type=StepType.VALIDATION,
                               ecu_id="A", description="c", estimated_seconds=5, depends_on=[2]),
        ],
    )
    result = compute_optimization(program)
    assert result.sequential_seconds == 15.0
    assert result.critical_path_seconds == 15.0
    assert result.speedup_factor == 1.0
    assert result.critical_path_steps == [1, 2, 3]


def test_full_pipeline_includes_optimization(simple_spec, monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "llm_provider", "mock")
    result = generate(simple_spec)
    assert result.optimization.sequential_seconds >= result.optimization.critical_path_seconds
    assert result.optimization.speedup_factor >= 1.0
