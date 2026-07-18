"""Tests for the mock generator and the end-to-end pipeline.

These run entirely offline (mock provider), so they are fast and deterministic.
"""

from __future__ import annotations

import pytest

from backend.config import settings
from backend.generator import generate
from backend.llm_service import MockProvider
from backend.models import CommissioningProgram, StepType


@pytest.fixture(autouse=True)
def force_mock_provider(monkeypatch):
    """Tests must be hermetic: never depend on a local .env's real API key.

    This forces the mock provider for every test in this module, regardless
    of what LLM_PROVIDER is set to in the developer's environment.
    """
    monkeypatch.setattr(settings, "llm_provider", "mock")


def test_mock_generates_valid_program(simple_spec):
    """The mock planner should produce a schema-valid, non-empty program."""
    raw = MockProvider().generate_program(simple_spec)
    program = CommissioningProgram.model_validate(raw)

    assert program.vehicle_id == simple_spec.vehicle_id
    assert len(program.steps) > 0

    # Steps must be numbered 1..N with no gaps.
    orders = [s.order for s in program.steps]
    assert orders == list(range(1, len(orders) + 1))


def test_security_access_precedes_flash(simple_spec):
    """For the ECU that needs flashing, unlock must come before the flash."""
    program = CommissioningProgram.model_validate(
        MockProvider().generate_program(simple_spec)
    )

    sec_order = next(
        s.order for s in program.steps
        if s.step_type == StepType.SECURITY_ACCESS and s.ecu_id == "BMS"
    )
    flash_order = next(
        s.order for s in program.steps
        if s.step_type == StepType.FLASH_SOFTWARE and s.ecu_id == "BMS"
    )
    assert sec_order < flash_order


def test_only_updating_ecu_is_flashed(simple_spec):
    """The gateway (same version) must not be flashed; the BMS must be."""
    program = CommissioningProgram.model_validate(
        MockProvider().generate_program(simple_spec)
    )
    flashed = {s.ecu_id for s in program.steps if s.step_type == StepType.FLASH_SOFTWARE}
    assert "BMS" in flashed
    assert "GATEWAY" not in flashed


def test_program_ends_with_clear_and_validation(simple_spec):
    """A well-formed program clears DTCs and validates at the end."""
    program = CommissioningProgram.model_validate(
        MockProvider().generate_program(simple_spec)
    )
    types = [s.step_type for s in program.steps]
    assert StepType.FAULT_CLEAR in types
    assert types[-1] == StepType.VALIDATION


def test_mock_never_asserts_unsupported_uds_service(simple_spec):
    """Regression test: the mock planner used to hardcode UDS 0x34 for
    flashing and 0x22 for per-ECU validation regardless of what the ECU
    actually declared as supported, which the evaluation harness surfaced as
    real (~70%) invalidity on randomised specs supporting only a subset of
    services. Every step's uds_service, if set, must be one the ECU supports.
    """
    ecu_by_id = {e.ecu_id: e for e in simple_spec.ecus}
    program = CommissioningProgram.model_validate(
        MockProvider().generate_program(simple_spec)
    )
    for step in program.steps:
        if step.uds_service is not None:
            assert step.uds_service in ecu_by_id[step.ecu_id].supported_uds_services


def test_mock_uses_whichever_flash_service_is_supported():
    """If an ECU only lists 0x36 (not 0x34), flashing must use 0x36."""
    from backend.models import Ecu, VehicleSpec

    spec = VehicleSpec(
        vehicle_id="V1",
        model="M",
        model_year=2026,
        ecus=[Ecu(
            ecu_id="A", name="A", part_number="PN-A",
            software_version="1", target_software_version="2",
            supported_uds_services=["0x10", "0x36"],  # no 0x34
        )],
    )
    program = CommissioningProgram.model_validate(MockProvider().generate_program(spec))
    flash_step = next(s for s in program.steps if s.step_type == StepType.FLASH_SOFTWARE)
    assert flash_step.uds_service == "0x36"


def test_full_pipeline_is_valid(simple_spec):
    """End-to-end: the generated program should pass validation cleanly."""
    result = generate(simple_spec)
    assert result.provider == "mock"
    assert result.is_valid is True
    # No error-severity issues.
    assert all(issue.severity != "error" for issue in result.validation)
    # Analytics are populated.
    assert result.analytics.total_steps == len(result.program.steps)
    assert result.analytics.estimated_cycle_time_seconds > 0
