"""Tests for program analytics."""

from __future__ import annotations

from backend.analytics import analyse_program
from backend.models import CommissioningProgram, CommissioningStep, StepType


def test_ecus_covered_ignores_steps_for_unknown_ecus(simple_spec):
    # See issue #23: a step referencing an ECU that isn't part of the spec
    # (stale/fabricated program) must not inflate ecus_covered past the
    # vehicle's actual ECU count.
    program = CommissioningProgram(
        vehicle_id=simple_spec.vehicle_id,
        steps=[
            CommissioningStep(
                order=1,
                step_type=StepType.DIAGNOSTIC_SESSION,
                ecu_id="BMS",  # known ECU
                description="Open session",
            ),
            CommissioningStep(
                order=2,
                step_type=StepType.DIAGNOSTIC_SESSION,
                ecu_id="NOT_A_REAL_ECU",  # unknown ECU
                description="Open session on bogus ECU",
            ),
        ],
    )
    analytics = analyse_program(simple_spec, program)

    assert analytics.ecus_covered == 1
    assert analytics.ecus_total == len(simple_spec.ecus)
    assert analytics.ecus_covered <= analytics.ecus_total


def test_ecus_covered_counts_each_known_ecu_once(simple_spec):
    program = CommissioningProgram(
        vehicle_id=simple_spec.vehicle_id,
        steps=[
            CommissioningStep(
                order=1,
                step_type=StepType.DIAGNOSTIC_SESSION,
                ecu_id="BMS",
                description="Open session",
            ),
            CommissioningStep(
                order=2,
                step_type=StepType.FLASH_SOFTWARE,
                ecu_id="BMS",
                description="Flash",
            ),
            CommissioningStep(
                order=3,
                step_type=StepType.DIAGNOSTIC_SESSION,
                ecu_id="GATEWAY",
                description="Open session",
            ),
        ],
    )
    analytics = analyse_program(simple_spec, program)

    assert analytics.ecus_covered == 2
