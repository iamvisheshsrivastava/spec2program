"""Export a commissioning program as a human-readable work-instruction CSV.

Issue #19: the only export this project produced was OTX XML, aimed at a
tooling/runtime consumer. A line technician or process engineer reviewing a
program on paper or in a spreadsheet needs a plain, ordered work-instruction
list instead - one row per step, with the fields that actually matter on the
floor (which ECU, which UDS service, what to do, how long it should take,
and what it waits on), not an XML document meant for a test executor.
"""

from __future__ import annotations

import csv
import io

from .models import CommissioningProgram

_FIELDNAMES = [
    "order",
    "step_type",
    "ecu_id",
    "uds_service",
    "description",
    "estimated_seconds",
    "depends_on",
]


def to_csv(program: CommissioningProgram) -> str:
    """Render a CommissioningProgram as a human-readable CSV work-instruction list."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    writer.writerow([f"# Commissioning program for vehicle: {program.vehicle_id}"])
    if program.notes:
        # Keep notes on their own commented line(s) rather than a column, so
        # the step rows below stay a clean, uniform table to import.
        for line in program.notes.splitlines() or [program.notes]:
            writer.writerow([f"# Notes: {line}"])
    writer.writerow([])
    writer.writerow(_FIELDNAMES)

    for step in sorted(program.steps, key=lambda s: s.order):
        writer.writerow([
            step.order,
            step.step_type.value,
            step.ecu_id,
            step.uds_service or "",
            step.description,
            step.estimated_seconds,
            ";".join(str(d) for d in step.depends_on),
        ])

    return buffer.getvalue()
