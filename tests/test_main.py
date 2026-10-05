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
    resp = client.post(
        "/api/export/otx",
        json={
            "spec": simple_spec.model_dump(mode="json"),
            "program": program.model_dump(mode="json"),
        },
    )
    assert resp.status_code == 400
    assert "malformed program structure" in resp.json()["detail"]


def test_export_otx_succeeds_for_a_valid_program(simple_spec):
    program = CommissioningProgram.model_validate(
        MockProvider().generate_program(simple_spec)
    )
    resp = client.post(
        "/api/export/otx",
        json={
            "spec": simple_spec.model_dump(mode="json"),
            "program": program.model_dump(mode="json"),
        },
    )
    assert resp.status_code == 200
    assert "<otx" in resp.text


def test_export_otx_sanitizes_vehicle_id_in_content_disposition(simple_spec, monkeypatch):
    # See issue #22: vehicle_id is attacker-controlled free text and was being
    # interpolated directly into the Content-Disposition header, allowing
    # header/CRLF injection (and a broken quoted-string via embedded `"`).
    monkeypatch.setattr("backend.main.validate_program", lambda spec, program: [])

    program = CommissioningProgram.model_validate(
        MockProvider().generate_program(simple_spec)
    )
    malicious_id = 'evil"\r\nX-Injected: 1\r\nSet-Cookie: a=b'
    program = program.model_copy(update={"vehicle_id": malicious_id})

    resp = client.post(
        "/api/export/otx",
        json={
            "spec": simple_spec.model_dump(mode="json"),
            "program": program.model_dump(mode="json"),
        },
    )
    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]
    assert "\r" not in disposition
    assert "\n" not in disposition
    assert '"' not in disposition.split("filename=", 1)[1][1:-1]
    assert "X-Injected" not in resp.headers
    assert disposition.startswith('attachment; filename="')
    assert disposition.endswith('_otx.xml"')


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


def test_recover_returns_400_for_unknown_failed_step_order(simple_spec):
    # See issue #17: an unknown failed_step_order is a client input error
    # (a bad/stale step order), and generate_recovery() raises a plain
    # ValueError for it - the route must map that to 400, not 500.
    program = CommissioningProgram.model_validate(
        MockProvider().generate_program(simple_spec)
    )
    resp = client.post(
        "/api/recover",
        json={
            "spec": simple_spec.model_dump(mode="json"),
            "program": program.model_dump(mode="json"),
            "failed_step_order": 999999,
            "failure_reason": "timeout",
        },
    )
    assert resp.status_code == 400
    assert "999999" in resp.json()["detail"]


def test_batch_rejects_oversized_request():
    from fastapi.testclient import TestClient

    from backend.main import app

    resp = TestClient(app).post("/api/batch", json={"specs": [{}] * 26})
    assert resp.status_code == 422


def test_generate_500_hides_upstream_error(monkeypatch):
    from fastapi.testclient import TestClient

    from backend import main

    def boom(_spec):
        raise RuntimeError("https://secret.upstream/url failed")

    monkeypatch.setattr(main, "generate", boom)
    client = TestClient(main.app, raise_server_exceptions=False)
    spec = client.get("/api/samples").json()[0]["file"]
    body = {"spec": client.get(f"/api/samples/{spec}").json()}
    resp = client.post("/api/generate", json=body)
    assert resp.status_code == 500
    assert "secret.upstream" not in resp.text
