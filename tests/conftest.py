"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from backend import db
from backend.config import settings
from backend.models import Ecu, VehicleSpec


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Clear the per-IP rate-limit counters between tests.

    All API tests share one TestClient hitting the same in-process app, so
    without this, request counts from an earlier test (or an earlier test
    file) would carry over and could trip the limiter in a later, unrelated
    test (issue #12's rate limiting).
    """
    from backend.main import limiter

    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture(autouse=True)
def isolated_history_db(tmp_path, monkeypatch):
    """Point the history database at a per-test temp file.

    Without this, every test that hits /api/generate, /api/batch, or
    /api/recover (or calls backend.db directly) would read/write the real
    data/spec2program.db on disk, leaking state between test runs and
    polluting the repo's data directory. Also resets the cached connection
    before and after each test so the new path actually takes effect.
    """
    db.reset_connection()
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "test_history.db"))
    yield
    db.reset_connection()


@pytest.fixture
def simple_spec() -> VehicleSpec:
    """A minimal two-ECU spec where one ECU needs a software update."""
    return VehicleSpec(
        vehicle_id="TEST-0001",
        model="TestCar",
        model_year=2026,
        configuration={"drivetrain": "BEV"},
        process_standards=["Security access required before flashing."],
        ecus=[
            Ecu(
                ecu_id="BMS",
                name="Battery Management System",
                part_number="PN-BMS-1",
                software_version="H12",
                target_software_version="H15",  # -> needs a flash
                supported_uds_services=["0x10", "0x27", "0x34", "0x36", "0x2E", "0x22", "0x14"],
            ),
            Ecu(
                ecu_id="GATEWAY",
                name="Central Gateway",
                part_number="PN-GW-1",
                software_version="2210",
                target_software_version="2210",  # -> no flash needed
                supported_uds_services=["0x10", "0x22", "0x14"],
            ),
        ],
    )
