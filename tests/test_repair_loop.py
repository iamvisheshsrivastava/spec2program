"""Tests for the self-repair loop in the generation pipeline.

Uses a small fake provider so the test is deterministic and offline: no real
LLM call, but it exercises exactly the same ``run_pipeline`` code path a real
provider would.
"""

from __future__ import annotations

from backend.generator import run_pipeline
from backend.models import ValidationIssue, VehicleSpec


class BrokenThenFixedProvider:
    """First call returns an invalid program (bad ECU reference); the repair
    call returns a valid one. Simulates an LLM that makes a mistake and then
    successfully corrects it when shown the validator's findings.
    """

    name = "fake-llm"

    def __init__(self, valid_program: dict, broken_program: dict):
        self.valid_program = valid_program
        self.broken_program = broken_program
        self.repair_calls = 0

    def generate_program(self, spec: VehicleSpec) -> dict:
        return self.broken_program

    def repair_program(
        self, spec: VehicleSpec, previous_program: dict, issues: list[ValidationIssue]
    ) -> dict:
        self.repair_calls += 1
        return self.valid_program


class AlwaysBrokenProvider:
    """Never produces a valid program, even after repair attempts."""

    name = "fake-llm-stubborn"

    def __init__(self, broken_program: dict):
        self.broken_program = broken_program
        self.repair_calls = 0

    def generate_program(self, spec: VehicleSpec) -> dict:
        return self.broken_program

    def repair_program(
        self, spec: VehicleSpec, previous_program: dict, issues: list[ValidationIssue]
    ) -> dict:
        self.repair_calls += 1
        return self.broken_program


def _valid_program_dict(spec: VehicleSpec) -> dict:
    return {
        "vehicle_id": spec.vehicle_id,
        "steps": [
            {
                "order": 1,
                "step_type": "diagnostic_session",
                "ecu_id": "BMS",
                "description": "Open session.",
                "uds_service": "0x10",
                "estimated_seconds": 3.0,
                "depends_on": [],
            },
        ],
        "notes": None,
    }


def _broken_program_dict(spec: VehicleSpec) -> dict:
    return {
        "vehicle_id": spec.vehicle_id,
        "steps": [
            {
                "order": 1,
                "step_type": "diagnostic_session",
                "ecu_id": "NOT_A_REAL_ECU",
                "description": "Open session on an ECU that doesn't exist.",
                "uds_service": "0x10",
                "estimated_seconds": 3.0,
                "depends_on": [],
            },
        ],
        "notes": None,
    }


def test_repair_loop_fixes_invalid_program(simple_spec):
    provider = BrokenThenFixedProvider(
        valid_program=_valid_program_dict(simple_spec),
        broken_program=_broken_program_dict(simple_spec),
    )
    result = run_pipeline(simple_spec, provider)

    assert provider.repair_calls == 1
    assert result.repair_attempts == 1
    assert result.is_valid is True
    assert "self-repair" in (result.program.notes or "").lower()


def test_repair_loop_gives_up_after_max_attempts(simple_spec):
    provider = AlwaysBrokenProvider(broken_program=_broken_program_dict(simple_spec))
    result = run_pipeline(simple_spec, provider, max_repair_attempts=2)

    assert provider.repair_calls == 2
    assert result.repair_attempts == 2
    assert result.is_valid is False
