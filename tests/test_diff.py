"""Tests for the program diff/compare feature (issue #20)."""

from __future__ import annotations

from backend.diff import diff_programs
from backend.models import CommissioningProgram, CommissioningStep, StepType


def _step(order, estimated_seconds=5.0, description="x", ecu_id="A", depends_on=None):
    return CommissioningStep(
        order=order, step_type=StepType.DIAGNOSTIC_SESSION, ecu_id=ecu_id,
        description=description, estimated_seconds=estimated_seconds,
        depends_on=depends_on or [],
    )


def test_identical_programs_have_no_changes():
    program = CommissioningProgram(vehicle_id="V1", steps=[_step(1), _step(2)])
    result = diff_programs(program, program)
    assert result["added"] == []
    assert result["removed"] == []
    assert result["modified"] == []
    assert result["unchanged_count"] == 2


def test_added_and_removed_steps_are_detected():
    a = CommissioningProgram(vehicle_id="V1", steps=[_step(1), _step(2)])
    b = CommissioningProgram(vehicle_id="V1", steps=[_step(1), _step(3)])
    result = diff_programs(a, b)
    assert result["removed"] == [2]
    assert result["added"] == [3]
    assert result["unchanged_count"] == 1


def test_modified_step_reports_changed_fields():
    a = CommissioningProgram(vehicle_id="V1", steps=[_step(1, estimated_seconds=5.0, description="old")])
    b = CommissioningProgram(vehicle_id="V1", steps=[_step(1, estimated_seconds=9.0, description="new")])
    result = diff_programs(a, b)
    assert len(result["modified"]) == 1
    changes = result["modified"][0]["changes"]
    assert changes["estimated_seconds"] == {"before": 5.0, "after": 9.0}
    assert changes["description"] == {"before": "old", "after": "new"}
    assert "ecu_id" not in changes


def test_vehicle_ids_are_reported_even_when_different():
    a = CommissioningProgram(vehicle_id="V1", steps=[_step(1)])
    b = CommissioningProgram(vehicle_id="V2", steps=[_step(1)])
    result = diff_programs(a, b)
    assert result["vehicle_id_a"] == "V1"
    assert result["vehicle_id_b"] == "V2"
