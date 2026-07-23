"""Tests for finite tester-channel scheduling."""

from __future__ import annotations

import pytest

from backend.models import CommissioningProgram, CommissioningStep, StepType
from backend.scheduler import channel_sweep, schedule_with_channels


def _program(steps):
    return CommissioningProgram(vehicle_id="V1", steps=steps)


def test_single_channel_equals_sequential_time():
    program = _program([
        CommissioningStep(order=1, step_type=StepType.DIAGNOSTIC_SESSION,
                           ecu_id="A", description="a", estimated_seconds=10, depends_on=[]),
        CommissioningStep(order=2, step_type=StepType.DIAGNOSTIC_SESSION,
                           ecu_id="B", description="b", estimated_seconds=10, depends_on=[]),
    ])
    result = schedule_with_channels(program, channels=1)
    assert result.cycle_time_seconds == 20.0
    assert result.channels == 1
    # Both steps land on the only channel, back-to-back.
    assert {s.channel for s in result.schedule} == {0}


def test_two_independent_steps_use_two_channels_in_parallel():
    program = _program([
        CommissioningStep(order=1, step_type=StepType.DIAGNOSTIC_SESSION,
                           ecu_id="A", description="a", estimated_seconds=10, depends_on=[]),
        CommissioningStep(order=2, step_type=StepType.DIAGNOSTIC_SESSION,
                           ecu_id="B", description="b", estimated_seconds=10, depends_on=[]),
    ])
    result = schedule_with_channels(program, channels=2)
    assert result.cycle_time_seconds == 10.0
    channels_used = {s.channel for s in result.schedule}
    assert channels_used == {0, 1}


def test_more_channels_than_steps_does_not_help_further():
    program = _program([
        CommissioningStep(order=1, step_type=StepType.DIAGNOSTIC_SESSION,
                           ecu_id="A", description="a", estimated_seconds=10, depends_on=[]),
        CommissioningStep(order=2, step_type=StepType.DIAGNOSTIC_SESSION,
                           ecu_id="B", description="b", estimated_seconds=10, depends_on=[]),
    ])
    two = schedule_with_channels(program, channels=2)
    five = schedule_with_channels(program, channels=5)
    assert two.cycle_time_seconds == five.cycle_time_seconds == 10.0


def test_dependent_chain_ignores_extra_channels():
    program = _program([
        CommissioningStep(order=1, step_type=StepType.DIAGNOSTIC_SESSION,
                           ecu_id="A", description="a", estimated_seconds=5, depends_on=[]),
        CommissioningStep(order=2, step_type=StepType.SECURITY_ACCESS,
                           ecu_id="A", description="b", estimated_seconds=5, depends_on=[1]),
    ])
    result = schedule_with_channels(program, channels=4)
    assert result.cycle_time_seconds == 10.0  # strictly sequential regardless of channels


def test_channel_sweep_is_monotonically_non_increasing():
    program = _program([
        CommissioningStep(order=i, step_type=StepType.DIAGNOSTIC_SESSION,
                           ecu_id=f"E{i}", description="x", estimated_seconds=5, depends_on=[])
        for i in range(1, 6)
    ])
    sweep = channel_sweep(program, max_channels=6)
    times = [p.cycle_time_seconds for p in sweep.points]
    assert times == sorted(times, reverse=True)
    assert len(times) == 6
    # 5 independent 5s steps: 1 channel -> 25s, 5+ channels -> 5s (floor).
    assert times[0] == 25.0
    assert times[-1] == 5.0


def test_empty_program_channel_schedule():
    program = _program([])
    result = schedule_with_channels(program, channels=3)
    assert result.cycle_time_seconds == 0.0
    assert result.schedule == []


def test_zero_or_negative_channels_raises():
    program = _program([
        CommissioningStep(order=1, step_type=StepType.DIAGNOSTIC_SESSION,
                           ecu_id="A", description="a", estimated_seconds=10, depends_on=[]),
    ])
    with pytest.raises(ValueError):
        schedule_with_channels(program, channels=0)
    with pytest.raises(ValueError):
        schedule_with_channels(program, channels=-5)
