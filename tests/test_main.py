"""API-level tests for error handling in the FastAPI routes."""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.llm_service import MockProvider
from backend.main import app
from backend.models import CommissioningProgram

client = TestClient(app)


def test_export_otx_returns_400_on_export_failure(simple_spec, monkeypatch):
    def _boom(program):
        raise RuntimeError("malformed program structure")

    monkeypatch.setattr("backend.main.to_otx_xml", _boom)

    program = CommissioningProgram.model_validate(
        MockProvider().generate_program(simple_spec)
    )
    resp = client.post("/api/export/otx", json=program.model_dump(mode="json"))
    assert resp.status_code == 400
    assert "malformed program structure" in resp.json()["detail"]


def test_export_otx_succeeds_for_a_valid_program(simple_spec):
    program = CommissioningProgram.model_validate(
        MockProvider().generate_program(simple_spec)
    )
    resp = client.post("/api/export/otx", json=program.model_dump(mode="json"))
    assert resp.status_code == 200
    assert "<otx" in resp.text


def test_optimize_channels_returns_400_for_invalid_channels(simple_spec):
    program = CommissioningProgram.model_validate(
        MockProvider().generate_program(simple_spec)
    )
    resp = client.post(
        "/api/optimize/channels",
        json={"program": program.model_dump(mode="json"), "channels": 0},
    )
    # Pydantic's ge=1 constraint on the request model rejects this before it
    # ever reaches schedule_with_channels, but either way it must not be a 500.
    assert resp.status_code in (400, 422)
