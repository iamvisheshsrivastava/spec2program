"""Export a commissioning program as a human-readable work-instruction CSV.

Issue #19: the only export this project produced was OTX XML, aimed at a
tooling/runtime consumer. A line technician or process engineer reviewing a
program on paper or in a spreadsheet needs a plain, ordered work-instruction
list instead - one row per step, with the fields that actually matter on the
floor (which ECU, which UDS service, what to do, how long it should take,
and what it waits on), not an XML document meant for a test executor.

Issue #25: several of those fields (``ecu_id``, ``uds_service``,
``description``) ultimately come from a vehicle spec or an LLM-generated
program, not from a trusted, fixed vocabulary. A cell value that happens to
start with ``=``, ``+``, ``-``, or ``@`` is interpreted as a formula by
Excel/LibreOffice/Google Sheets when the CSV is opened, which is the
standard "CSV/formula injection" vector. Any such value is prefixed with a
leading apostrophe before being written, which spreadsheet applications
render as a literal, inert text cell instead of evaluating it.
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

# Leading characters that spreadsheet applications treat as the start of a
# formula when a CSV cell is opened.
_FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@")


def _sanitize_cell(value: str) -> str:
    """Neutralise a value that would be interpreted as a spreadsheet formula.

    Prefixing with a leading apostrophe is the standard CSV-injection
    mitigation: spreadsheet applications display the value as literal text
    (stripping the apostrophe) instead of evaluating it as a formula.
    """
    if value and value[0] in _FORMULA_TRIGGER_CHARS:
        return "'" + value
    return value


def to_csv(program: CommissioningProgram) -> str:
    """Render a CommissioningProgram as a human-readable CSV work-instruction list."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    writer.writerow([_sanitize_cell(f"# Commissioning program for vehicle: {program.vehicle_id}")])
    if program.notes:
        # Keep notes on their own commented line(s) rather than a column, so
        # the step rows below stay a clean, uniform table to import.
        for line in program.notes.splitlines() or [program.notes]:
            writer.writerow([_sanitize_cell(f"# Notes: {line}")])
    writer.writerow([])
    writer.writerow(_FIELDNAMES)

    for step in sorted(program.steps, key=lambda s: s.order):
        writer.writerow([
            step.order,
            step.step_type.value,
            _sanitize_cell(step.ecu_id),
            _sanitize_cell(step.uds_service or ""),
            _sanitize_cell(step.description),
            step.estimated_seconds,
            ";".join(str(d) for d in step.depends_on),
        ])

    return buffer.getvalue()
