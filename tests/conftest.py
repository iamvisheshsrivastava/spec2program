"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from backend.models import Ecu, VehicleSpec


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
