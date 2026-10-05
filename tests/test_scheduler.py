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


def test_forward_dependency_raises_instead_of_being_dropped():
    """Issue #7 regression: a step depending on a *later* order must not be
    silently skipped - it must be honoured via topological sort, or the
    cycle it creates must raise loudly.
    """

    program = CommissioningProgram(
        vehicle_id="V1",
        steps=[
            CommissioningStep(order=1, step_type=StepType.DIAGNOSTIC_SESSION,
                               ecu_id="A", description="a", estimated_seconds=5, depends_on=[2]),
            CommissioningStep(order=2, step_type=StepType.DIAGNOSTIC_SESSION,
                               ecu_id="A", description="b", estimated_seconds=5, depends_on=[]),
        ],
    )
    # Step 1 depends on step 2, which does not depend on step 1 - this is a
    # valid DAG (just not in declared-order order), so it must now be
    # honoured: critical path is 10s (step 2 then step 1), not 5s.
    result = compute_optimization(program)
    assert result.critical_path_seconds == 10.0
    assert result.critical_path_steps == [2, 1]


def test_cyclic_dependency_raises_value_error():
    program = CommissioningProgram(
        vehicle_id="V1",
        steps=[
            CommissioningStep(order=1, step_type=StepType.DIAGNOSTIC_SESSION,
                               ecu_id="A", description="a", estimated_seconds=5, depends_on=[2]),
            CommissioningStep(order=2, step_type=StepType.DIAGNOSTIC_SESSION,
                               ecu_id="A", description="b", estimated_seconds=5, depends_on=[1]),
        ],
    )
    import pytest
    with pytest.raises(ValueError):
        compute_optimization(program)


def test_dangling_dependency_raises_value_error():
    program = CommissioningProgram(
        vehicle_id="V1",
        steps=[
            CommissioningStep(order=1, step_type=StepType.DIAGNOSTIC_SESSION,
                               ecu_id="A", description="a", estimated_seconds=5, depends_on=[99]),
        ],
    )
    import pytest
    with pytest.raises(ValueError):
        compute_optimization(program)


def test_invalid_dependency_graph_reported_as_validation_error(simple_spec, monkeypatch):
    """The generation pipeline must not crash nor report plausible-looking
    optimization numbers for a program whose dependency graph is cyclic -
    it should surface an explicit error issue instead (issue #7).
    """
    from backend.config import settings
    from backend.generator import run_pipeline
    from backend.llm_service import LLMProvider

    class _CyclicProvider(LLMProvider):
        name = "cyclic-test"

        def generate_program(self, spec):
            return {
                "vehicle_id": spec.vehicle_id,
                "steps": [
                    {"order": 1, "step_type": "diagnostic_session", "ecu_id": "BMS",
                     "description": "a", "estimated_seconds": 5, "depends_on": [2]},
                    {"order": 2, "step_type": "diagnostic_session", "ecu_id": "BMS",
                     "description": "b", "estimated_seconds": 5, "depends_on": [1]},
                ],
            }

    monkeypatch.setattr(settings, "llm_provider", "mock")
    result = run_pipeline(simple_spec, _CyclicProvider())
    assert result.is_valid is False
    assert result.optimization.critical_path_seconds == 0.0
    assert any("dependency graph" in i.message.lower() for i in result.validation)
