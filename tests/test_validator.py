"""Tests for the rule-based validator.

We deliberately hand-craft *broken* programs to prove the validator catches
the failure modes that matter in a plant: unknown ECUs, unsupported UDS
services, and flashing before security access.
"""

from __future__ import annotations

from backend.models import CommissioningProgram, CommissioningStep, StepType
from backend.validator import is_valid, validate_program


def test_valid_program_has_no_errors(simple_spec):
    """A correct minimal program should produce no error-severity issues."""
    program = CommissioningProgram(
        vehicle_id="TEST-0001",
        steps=[
            CommissioningStep(order=1, step_type=StepType.DIAGNOSTIC_SESSION,
                              ecu_id="BMS", description="open session",
                              uds_service="0x10", estimated_seconds=3),
            CommissioningStep(order=2, step_type=StepType.SECURITY_ACCESS,
                              ecu_id="BMS", description="unlock",
                              uds_service="0x27", estimated_seconds=4, depends_on=[1]),
            CommissioningStep(order=3, step_type=StepType.FLASH_SOFTWARE,
                              ecu_id="BMS", description="flash",
                              uds_service="0x34", estimated_seconds=45, depends_on=[2]),
            CommissioningStep(order=4, step_type=StepType.VALIDATION,
                              ecu_id="BMS", description="validate",
                              uds_service="0x22", estimated_seconds=8, depends_on=[3]),
            CommissioningStep(order=5, step_type=StepType.FAULT_CLEAR,
                              ecu_id="BMS", description="clear",
                              uds_service="0x14", estimated_seconds=5, depends_on=[4]),
        ],
    )
    issues = validate_program(simple_spec, program)
    assert is_valid(issues)


def test_unknown_ecu_is_flagged(simple_spec):
    """A step targeting an ECU not in the spec must be an error."""
    program = CommissioningProgram(
        vehicle_id="TEST-0001",
        steps=[
            CommissioningStep(order=1, step_type=StepType.DIAGNOSTIC_SESSION,
                              ecu_id="DOES_NOT_EXIST", description="x",
                              uds_service="0x10", estimated_seconds=3),
        ],
    )
    issues = validate_program(simple_spec, program)
    assert not is_valid(issues)
    assert any("not in the vehicle specification" in i.message for i in issues)


def test_unsupported_uds_service_is_flagged(simple_spec):
    """Using a UDS service the ECU does not support must be an error."""
    program = CommissioningProgram(
        vehicle_id="TEST-0001",
        steps=[
            # GATEWAY does not support 0x27 (security access).
            CommissioningStep(order=1, step_type=StepType.SECURITY_ACCESS,
                              ecu_id="GATEWAY", description="bad unlock",
                              uds_service="0x27", estimated_seconds=4),
        ],
    )
    issues = validate_program(simple_spec, program)
    assert not is_valid(issues)
    assert any("does not support" in i.message for i in issues)


def test_flash_before_security_access_is_flagged(simple_spec):
    """Flashing an ECU before unlocking it must be an error."""
    program = CommissioningProgram(
        vehicle_id="TEST-0001",
        steps=[
            CommissioningStep(order=1, step_type=StepType.FLASH_SOFTWARE,
                              ecu_id="BMS", description="premature flash",
                              uds_service="0x34", estimated_seconds=45),
        ],
    )
    issues = validate_program(simple_spec, program)
    assert not is_valid(issues)
    assert any("before a security-access step" in i.message for i in issues)


def test_vehicle_id_mismatch_is_flagged(simple_spec):
    """A program whose vehicle_id does not match the spec must be an error."""
    program = CommissioningProgram(vehicle_id="WRONG-ID", steps=[
        CommissioningStep(order=1, step_type=StepType.DIAGNOSTIC_SESSION,
                          ecu_id="BMS", description="x",
                          uds_service="0x10", estimated_seconds=3),
    ])
    issues = validate_program(simple_spec, program)
    assert not is_valid(issues)
    assert any("does not match" in i.message for i in issues)
