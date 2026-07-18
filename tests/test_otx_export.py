"""Tests for the OTX-style export."""

from __future__ import annotations

from backend.llm_service import MockProvider
from backend.models import CommissioningProgram
from backend.otx_export import to_otx_xml


def test_otx_export_contains_every_step(simple_spec):
    program = CommissioningProgram.model_validate(
        MockProvider().generate_program(simple_spec)
    )
    xml = to_otx_xml(program)

    assert "<otx" in xml
    assert f'id="proc_{simple_spec.vehicle_id}"' in xml
    for step in program.steps:
        assert f'id="step_{step.order}"' in xml
        assert f"<target-ecu>{step.ecu_id}</target-ecu>" in xml
