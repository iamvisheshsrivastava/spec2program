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
