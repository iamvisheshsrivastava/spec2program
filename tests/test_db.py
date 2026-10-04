"""Tests for the SQLite history/persistence layer (issue #16)."""

from __future__ import annotations

from backend import db


def test_record_and_list_history_round_trip():
    item_id = db.record_history(
        kind="generate", vehicle_id="V1", provider="mock", is_valid=True,
        request_json='{"spec": {}}', response_json='{"program": {}}',
    )
    assert item_id > 0

    rows = db.list_history(kind="generate")
    assert any(r["id"] == item_id and r["vehicle_id"] == "V1" for r in rows)


def test_get_history_item_returns_full_payload():
    item_id = db.record_history(
        kind="recover", vehicle_id="V2", provider="mock-fallback", is_valid=False,
        request_json='{"failure_reason": "timeout"}', response_json='{"is_valid": false}',
    )
    record = db.get_history_item(item_id)
    assert record is not None
    assert record["kind"] == "recover"
    assert record["request"] == {"failure_reason": "timeout"}
    assert record["response"] == {"is_valid": False}


def test_get_history_item_missing_returns_none():
    assert db.get_history_item(999999) is None


def test_list_history_filters_by_vehicle_id():
    db.record_history(
        kind="generate", vehicle_id="FLEET-A", provider="mock", is_valid=True,
        request_json="{}", response_json="{}",
    )
    db.record_history(
        kind="generate", vehicle_id="FLEET-B", provider="mock", is_valid=True,
        request_json="{}", response_json="{}",
    )
    rows = db.list_history(vehicle_id="FLEET-A")
    assert all(r["vehicle_id"] == "FLEET-A" for r in rows)
    assert len(rows) >= 1


def test_list_history_respects_limit():
    for _ in range(5):
        db.record_history(
            kind="generate", vehicle_id="LIMIT-TEST", provider="mock", is_valid=True,
            request_json="{}", response_json="{}",
        )
    rows = db.list_history(vehicle_id="LIMIT-TEST", limit=2)
    assert len(rows) == 2


def test_migration_is_idempotent_across_reconnects():
    """Closing and reopening the connection must not error or duplicate the schema."""
    db.record_history(
        kind="generate", vehicle_id="RECONNECT", provider="mock", is_valid=True,
        request_json="{}", response_json="{}",
    )
    db.reset_connection()
    rows = db.list_history(vehicle_id="RECONNECT")
    assert len(rows) == 1
