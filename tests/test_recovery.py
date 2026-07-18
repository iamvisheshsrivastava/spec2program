"""Tests for runtime corrective-action recovery."""

from __future__ import annotations

import pytest

from backend.config import settings
from backend.models import RecoveryRequest
from backend.recovery import generate_recovery


@pytest.fixture(autouse=True)
def force_mock_provider(monkeypatch):
    """No real LLM configured -> generate_recovery_program() raises ->
    recovery.py falls back to the deterministic mock policy. Hermetic and
    fast, and exercises the fallback path every other test in this project
    also relies on.
    """
    monkeypatch.setattr(settings, "llm_provider", "mock")


def _program_dict(spec_id: str):
    from backend.models import CommissioningProgram, CommissioningStep, StepType
    return CommissioningProgram(
        vehicle_id=spec_id,
        steps=[
            CommissioningStep(order=1, step_type=StepType.DIAGNOSTIC_SESSION,
                               ecu_id="BMS", description="Open session", uds_service="0x10",
                               estimated_seconds=3.0, depends_on=[]),
            CommissioningStep(order=2, step_type=StepType.SECURITY_ACCESS,
                               ecu_id="BMS", description="Unlock", uds_service="0x27",
                               estimated_seconds=4.0, depends_on=[1]),
            CommissioningStep(order=3, step_type=StepType.FLASH_SOFTWARE,
                               ecu_id="BMS", description="Flash H12->H15", uds_service="0x34",
                               estimated_seconds=32.0, depends_on=[2]),
        ],
    )


def test_communication_failure_reopens_session_then_retries(simple_spec):
    program = _program_dict(simple_spec.vehicle_id)
    request = RecoveryRequest(
        spec=simple_spec, program=program, failed_step_order=3,
        failure_reason="communication timeout",
    )
    response = generate_recovery(request)

    assert response.provider == "mock-fallback"
    assert response.is_valid is True
    step_types = [s.step_type.value for s in response.recovery_steps]
    # Re-open session first, retry the flash, then confirm with a validation.
    assert step_types == ["diagnostic_session", "flash_software", "validation"]


def test_security_failure_reestablishes_access_then_retries(simple_spec):
    program = _program_dict(simple_spec.vehicle_id)
    request = RecoveryRequest(
        spec=simple_spec, program=program, failed_step_order=3,
        failure_reason="security access denied",
    )
    response = generate_recovery(request)

    assert response.is_valid is True
    step_types = [s.step_type.value for s in response.recovery_steps]
    assert step_types == ["security_access", "flash_software", "validation"]


def test_recovery_steps_reference_valid_uds_services(simple_spec):
    program = _program_dict(simple_spec.vehicle_id)
    request = RecoveryRequest(
        spec=simple_spec, program=program, failed_step_order=3,
        failure_reason="flash verification failed",
    )
    response = generate_recovery(request)

    ecu_by_id = {e.ecu_id: e for e in simple_spec.ecus}
    for step in response.recovery_steps:
        if step.uds_service:
            assert step.uds_service in ecu_by_id[step.ecu_id].supported_uds_services
    assert all(issue.severity != "error" for issue in response.validation)


def test_unknown_failed_step_order_raises(simple_spec):
    program = _program_dict(simple_spec.vehicle_id)
    request = RecoveryRequest(
        spec=simple_spec, program=program, failed_step_order=999,
        failure_reason="anything",
    )
    with pytest.raises(ValueError):
        generate_recovery(request)


def test_recovery_step_numbering_continues_after_program(simple_spec):
    program = _program_dict(simple_spec.vehicle_id)
    request = RecoveryRequest(
        spec=simple_spec, program=program, failed_step_order=3,
        failure_reason="timeout",
    )
    response = generate_recovery(request)
    assert all(s.order > len(program.steps) for s in response.recovery_steps)
