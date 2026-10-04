"""API-level tests for the CSV export, compare, history, and rate-limiting
endpoints added for issues #19, #20, #16, and #12."""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.config import settings
from backend.llm_service import MockProvider
from backend.main import app
from backend.models import CommissioningProgram

client = TestClient(app)


def test_export_csv_succeeds_for_a_valid_program(simple_spec):
    program = CommissioningProgram.model_validate(MockProvider().generate_program(simple_spec))
    resp = client.post(
        "/api/export/csv",
        json={
            "spec": simple_spec.model_dump(mode="json"),
            "program": program.model_dump(mode="json"),
        },
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "order,step_type,ecu_id" in resp.text


def test_export_csv_rejects_invalid_program(simple_spec):
    bad_program = CommissioningProgram(vehicle_id=simple_spec.vehicle_id, steps=[])
    resp = client.post(
        "/api/export/csv",
        json={
            "spec": simple_spec.model_dump(mode="json"),
            "program": bad_program.model_dump(mode="json"),
        },
    )
    assert resp.status_code == 400


def test_compare_endpoint_reports_diff(simple_spec):
    program = CommissioningProgram.model_validate(MockProvider().generate_program(simple_spec))
    other = program.model_copy(deep=True)
    other.steps[0].description = "Changed description"
    resp = client.post(
        "/api/compare",
        json={
            "program_a": program.model_dump(mode="json"),
            "program_b": other.model_dump(mode="json"),
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["modified"]) == 1
    assert body["modified"][0]["order"] == program.steps[0].order


def test_generate_is_persisted_and_retrievable_from_history(simple_spec, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "mock")
    resp = client.post("/api/generate", json={"spec": simple_spec.model_dump(mode="json")})
    assert resp.status_code == 200

    history_resp = client.get("/api/history", params={"kind": "generate", "vehicle_id": simple_spec.vehicle_id})
    assert history_resp.status_code == 200
    rows = history_resp.json()
    assert len(rows) >= 1
    item_id = rows[0]["id"]

    detail_resp = client.get(f"/api/history/{item_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["kind"] == "generate"
    assert "response" in detail and "request" in detail


def test_history_item_not_found_returns_404():
    resp = client.get("/api/history/999999999")
    assert resp.status_code == 404


def test_history_rejects_invalid_kind_filter():
    resp = client.get("/api/history", params={"kind": "not-a-real-kind"})
    assert resp.status_code == 400


def test_generate_endpoint_is_rate_limited(simple_spec, monkeypatch):
    """Issue #12: the LLM-credit-spending endpoints must reject bursts
    beyond the configured per-IP limit rather than passing every one through.
    """
    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(settings, "rate_limit_llm_endpoints", "2/minute")

    body = {"spec": simple_spec.model_dump(mode="json")}
    statuses = [client.post("/api/generate", json=body).status_code for _ in range(4)]
    assert statuses.count(200) <= 2
    assert 429 in statuses
