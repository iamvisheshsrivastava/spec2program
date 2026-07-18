"""Tests for VehicleSpec/Ecu schema tolerance.

Real-world BOM/PLM exports are messier than a hand-written sample: they carry
extra columns we don't model, and configuration values that are naturally
booleans or numbers, not just strings. A spec should never be rejected for
those reasons - only for genuinely missing/malformed required data. This
guards against a regression of a real bug found via manual testing: a spec
with `"left_hand_drive": true` in `configuration` was rejected with a 422
because the field was typed `dict[str, str]`.
"""

from __future__ import annotations

from backend.models import Ecu, VehicleSpec


def test_configuration_accepts_mixed_value_types():
    """Boolean/int/float configuration values must not be rejected."""
    spec = VehicleSpec(
        vehicle_id="V1",
        model="TestModel",
        model_year=2026,
        configuration={
            "drivetrain": "ICE",          # str
            "left_hand_drive": True,      # bool
            "seat_count": 5,               # int
            "battery_voltage": 12.6,       # float
        },
    )
    assert spec.configuration["left_hand_drive"] is True
    assert spec.configuration["seat_count"] == 5
    assert spec.configuration["battery_voltage"] == 12.6


def test_vehicle_spec_ignores_unknown_top_level_fields():
    """Extra PLM/BOM export fields (e.g. plant, vin) must not cause a 422."""
    spec = VehicleSpec(
        vehicle_id="V1",
        model="TestModel",
        model_year=2026,
        plant="Wolfsburg",  # not a modelled field
        vin="WVWZZZAUZGP123456",  # not a modelled field
    )
    assert spec.vehicle_id == "V1"
    assert not hasattr(spec, "plant")


def test_ecu_ignores_unknown_fields():
    """Extra per-ECU flags (e.g. flash_required) must not cause a 422."""
    ecu = Ecu(
        ecu_id="ECM",
        name="Engine Control Module",
        part_number="06L-906-026",
        flash_required=False,  # not a modelled field
        coding_required=True,  # not a modelled field
    )
    assert ecu.ecu_id == "ECM"
