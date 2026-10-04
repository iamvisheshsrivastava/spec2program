"""Tests for the offline batch-generation CLI (issue #21)."""

from __future__ import annotations

import json

from backend import cli
from backend.config import settings


def _write_spec(tmp_path, spec_model, name="spec.json"):
    path = tmp_path / name
    path.write_text(spec_model.model_dump_json(), encoding="utf-8")
    return str(path)


def test_cli_generate_writes_valid_program(tmp_path, simple_spec, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "mock")
    spec_path = _write_spec(tmp_path, simple_spec)
    out_path = str(tmp_path / "out.json")

    rc = cli.main(["generate", spec_path, "-o", out_path])
    assert rc == 0
    result = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert result["program"]["vehicle_id"] == simple_spec.vehicle_id
    assert result["is_valid"] is True


def test_cli_batch_writes_aggregate(tmp_path, simple_spec, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "mock")
    specs_path = tmp_path / "specs.json"
    specs_path.write_text(
        json.dumps([simple_spec.model_dump(mode="json"), simple_spec.model_dump(mode="json")]),
        encoding="utf-8",
    )
    out_path = str(tmp_path / "batch_out.json")

    rc = cli.main(["batch", str(specs_path), "-o", out_path])
    assert rc == 0
    result = json.loads((tmp_path / "batch_out.json").read_text(encoding="utf-8"))
    assert result["aggregate"]["vehicles"] == 2


def test_cli_export_otx_and_csv_roundtrip(tmp_path, simple_spec, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "mock")
    spec_path = _write_spec(tmp_path, simple_spec)
    program_path = str(tmp_path / "program.json")
    cli.main(["generate", spec_path, "-o", program_path])

    xml_path = str(tmp_path / "program.xml")
    rc_xml = cli.main(["export-otx", program_path, spec_path, "-o", xml_path])
    assert rc_xml == 0
    assert "<otx" in (tmp_path / "program.xml").read_text(encoding="utf-8")

    csv_path = str(tmp_path / "program.csv")
    rc_csv = cli.main(["export-csv", program_path, spec_path, "-o", csv_path])
    assert rc_csv == 0
    assert "order,step_type" in (tmp_path / "program.csv").read_text(encoding="utf-8")


def test_cli_compare_two_programs(tmp_path, simple_spec, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "mock")
    spec_path = _write_spec(tmp_path, simple_spec)
    program_a_path = str(tmp_path / "a.json")
    program_b_path = str(tmp_path / "b.json")
    cli.main(["generate", spec_path, "-o", program_a_path])
    cli.main(["generate", spec_path, "-o", program_b_path])

    diff_path = str(tmp_path / "diff.json")
    rc = cli.main(["compare", program_a_path, program_b_path, "-o", diff_path])
    assert rc == 0
    diff = json.loads((tmp_path / "diff.json").read_text(encoding="utf-8"))
    assert "added" in diff and "removed" in diff and "modified" in diff
