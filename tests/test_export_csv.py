"""Tests for the human-readable CSV work-instruction export (issue #19)."""

from __future__ import annotations

import csv
import io

from backend.export_csv import to_csv
from backend.models import CommissioningProgram, CommissioningStep, StepType


def _program():
    return CommissioningProgram(
        vehicle_id="V1",
        notes="Fell back to mock.",
        steps=[
            CommissioningStep(order=1, step_type=StepType.DIAGNOSTIC_SESSION,
                               ecu_id="BMS", description="Open session", uds_service="0x10",
                               estimated_seconds=3.0, depends_on=[]),
            CommissioningStep(order=2, step_type=StepType.SECURITY_ACCESS,
                               ecu_id="BMS", description="Unlock", uds_service="0x27",
                               estimated_seconds=4.0, depends_on=[1]),
        ],
    )


def test_csv_contains_header_and_one_row_per_step():
    text = to_csv(_program())
    rows = list(csv.reader(io.StringIO(text)))
    data_rows = [r for r in rows if r and r[0].isdigit()]
    assert len(data_rows) == 2
    assert data_rows[0][0] == "1"
    assert data_rows[1][2] == "BMS"


def test_csv_includes_vehicle_id_and_notes_as_comments():
    text = to_csv(_program())
    assert "V1" in text
    assert "Fell back to mock." in text


def test_csv_depends_on_is_semicolon_joined():
    text = to_csv(_program())
    rows = list(csv.reader(io.StringIO(text)))
    header_idx = rows.index(["order", "step_type", "ecu_id", "uds_service",
                              "description", "estimated_seconds", "depends_on"])
    second_data_row = rows[header_idx + 2]
    assert second_data_row[-1] == "1"


def test_empty_program_still_has_header():
    text = to_csv(CommissioningProgram(vehicle_id="V2", steps=[]))
    assert "order" in text
