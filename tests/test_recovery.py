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


def test_empty_recovery_is_reported_as_invalid_not_vacuously_valid(simple_spec):
    """Issue #9 regression: the failed step's ECU is not in the spec at all,
    so the mock policy has nothing to recover onto. This must come back as
    an explicit failure (is_valid False, an error-severity issue), never as
    a green 'recovery passed' response with an empty steps list.
    """
    from backend.models import CommissioningProgram, CommissioningStep, StepType

    program = CommissioningProgram(
        vehicle_id=simple_spec.vehicle_id,
        steps=[
            CommissioningStep(order=1, step_type=StepType.FLASH_SOFTWARE,
                               ecu_id="GHOST", description="Flash ghost ecu",
                               uds_service="0x34", estimated_seconds=10.0, depends_on=[]),
        ],
    )
    request = RecoveryRequest(
        spec=simple_spec, program=program, failed_step_order=1,
        failure_reason="communication timeout",
    )
    response = generate_recovery(request)

    assert response.recovery_steps == []
    assert response.is_valid is False
    assert any(issue.severity == "error" for issue in response.validation)


def test_llm_returning_zero_steps_falls_back_to_mock_policy(simple_spec, monkeypatch):
    """Issue #9 regression: a model that returns {"steps": []} must not be
    treated as a successful, empty recovery - it should get the same
    fallback-to-mock-policy treatment as an LLM call that raised.
    """
    from backend import recovery as recovery_module

    program = _program_dict(simple_spec.vehicle_id)

    def _empty_llm(*args, **kwargs):
        return {"steps": [], "notes": "nothing to report"}, "fake-llm"

    monkeypatch.setattr(recovery_module, "generate_recovery_program", _empty_llm)

    request = RecoveryRequest(
        spec=simple_spec, program=program, failed_step_order=3,
        failure_reason="communication timeout",
    )
    response = generate_recovery(request)

    assert response.provider == "mock-fallback"
    assert response.recovery_steps  # the deterministic policy actually ran
    assert response.is_valid is True


def test_recovery_step_numbering_does_not_collide_with_gapped_orders(simple_spec):
    """Regression test: order used to be based on len(steps), which collides
    with an existing step's order whenever the program has gaps (e.g. after a
    prior partial recovery round leaves orders 1, 2, 4 instead of 1, 2, 3).
    """
    from backend.models import CommissioningProgram, CommissioningStep, StepType

    program = CommissioningProgram(
        vehicle_id=simple_spec.vehicle_id,
        steps=[
            CommissioningStep(order=1, step_type=StepType.DIAGNOSTIC_SESSION,
                               ecu_id="BMS", description="Open session", uds_service="0x10",
                               estimated_seconds=3.0, depends_on=[]),
            CommissioningStep(order=2, step_type=StepType.SECURITY_ACCESS,
                               ecu_id="BMS", description="Unlock", uds_service="0x27",
                               estimated_seconds=4.0, depends_on=[1]),
            # Gap: order 3 is missing (e.g. removed by an earlier edit).
            CommissioningStep(order=4, step_type=StepType.FLASH_SOFTWARE,
                               ecu_id="BMS", description="Flash H12->H15", uds_service="0x34",
                               estimated_seconds=32.0, depends_on=[2]),
        ],
    )
    request = RecoveryRequest(
        spec=simple_spec, program=program, failed_step_order=4,
        failure_reason="timeout",
    )
    response = generate_recovery(request)

    existing_orders = {s.order for s in program.steps}
    new_orders = [s.order for s in response.recovery_steps]
    assert not existing_orders.intersection(new_orders)
    assert len(new_orders) == len(set(new_orders))
