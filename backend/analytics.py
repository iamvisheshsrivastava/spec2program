"""Program analytics.

The PhD project is explicitly about *optimisation potential* - cycle time,
program structure, corrective actions. This module turns a generated program
into quantitative metrics that surface that potential: total estimated cycle
time, a breakdown of steps by type, ECU coverage, and how many steps could in
principle run in parallel (a lever for reducing cycle time).
"""

from __future__ import annotations

from collections import Counter

from .models import CommissioningProgram, ProgramAnalytics, VehicleSpec


def analyse_program(
    spec: VehicleSpec, program: CommissioningProgram
) -> ProgramAnalytics:
    """Compute quantitative metrics for a commissioning program."""

    # Total estimated cycle time = sum of per-step time budgets. In a real
    # plant this is the headline KPI the process engineer wants to minimise.
    total_time = sum(step.estimated_seconds for step in program.steps)

    # Distribution of work across step types (e.g. how much is flashing).
    by_type = Counter(step.step_type.value for step in program.steps)

    # ECU coverage: how many of the vehicle's ECUs the program actually touches.
    # Intersect against the spec's known ECU ids so a step referencing an
    # ECU that isn't part of the vehicle (e.g. a stale/fabricated program -
    # see issue #23) can't inflate coverage above ecus_total.
    known_ecu_ids = {ecu.ecu_id for ecu in spec.ecus}
    ecus_covered = len({step.ecu_id for step in program.steps} & known_ecu_ids)

    # Parallelisation headroom: steps that declare no dependencies could, in
    # principle, be executed concurrently on multi-channel tester hardware.
    # Reporting this points at a concrete optimisation lever.
    parallelisable = sum(1 for step in program.steps if not step.depends_on)

    return ProgramAnalytics(
        total_steps=len(program.steps),
        estimated_cycle_time_seconds=round(total_time, 1),
        steps_by_type=dict(by_type),
        ecus_covered=ecus_covered,
        ecus_total=len(spec.ecus),
        parallelisable_steps=parallelisable,
    )
