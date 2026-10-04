"""Command-line interface for local batch generation and export (issue #21).

Everything in this project previously required running the FastAPI server
and talking HTTP to it, even for a purely offline, single-machine batch run.
This CLI calls the exact same pipeline (``backend.generator.run_pipeline`` /
``backend.batch.run_batch``) directly, in-process, with no server needed -
useful for CI, local scripting, or generating/exporting a fleet's worth of
programs from the shell.

Usage::

    python -m backend.cli generate spec.json [-o program.json]
    python -m backend.cli batch specs.json [-o results.json]
    python -m backend.cli export-otx program.json spec.json [-o program.xml]
    python -m backend.cli export-csv program.json spec.json [-o program.csv]
    python -m backend.cli compare program_a.json program_b.json [-o diff.json]

``spec.json`` must be a single ``VehicleSpec``-shaped object. ``specs.json``
for ``batch`` must be a JSON array of ``VehicleSpec`` objects. Export/compare
commands take program JSON produced by ``generate``/``batch`` (an object
with a ``"program"`` key, or a bare ``CommissioningProgram``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .batch import run_batch
from .diff import diff_programs
from .export_csv import to_csv
from .generator import generate
from .models import CommissioningProgram, VehicleSpec
from .otx_export import to_otx_xml
from .validator import is_valid, validate_program


def _read_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_output(text: str, out: str | None) -> None:
    if out:
        Path(out).write_text(text, encoding="utf-8")
        print(f"Wrote {out}", file=sys.stderr)
    else:
        print(text)


def _load_program(path: str) -> CommissioningProgram:
    """Load a CommissioningProgram from either a bare program JSON file or a
    GenerateResponse-shaped file (one with a top-level "program" key)."""
    raw = _read_json(path)
    if "program" in raw and "steps" not in raw:
        raw = raw["program"]
    return CommissioningProgram.model_validate(raw)


def cmd_generate(args: argparse.Namespace) -> int:
    spec = VehicleSpec.model_validate(_read_json(args.spec))
    result = generate(spec)
    _write_output(result.model_dump_json(indent=2), args.output)
    return 0 if result.is_valid else 1


def cmd_batch(args: argparse.Namespace) -> int:
    raw_specs = _read_json(args.specs)
    if not isinstance(raw_specs, list):
        print("error: specs file must contain a JSON array of VehicleSpec objects", file=sys.stderr)
        return 2
    specs = [VehicleSpec.model_validate(s) for s in raw_specs]
    result = run_batch(specs)
    _write_output(result.model_dump_json(indent=2), args.output)
    return 0 if result.aggregate.valid_count == result.aggregate.vehicles else 1


def cmd_export_otx(args: argparse.Namespace) -> int:
    program = _load_program(args.program)
    spec = VehicleSpec.model_validate(_read_json(args.spec))
    issues = validate_program(spec, program)
    if not is_valid(issues):
        for issue in issues:
            if issue.severity == "error":
                print(f"error: {issue.message}", file=sys.stderr)
        return 1
    _write_output(to_otx_xml(program), args.output)
    return 0


def cmd_export_csv(args: argparse.Namespace) -> int:
    program = _load_program(args.program)
    spec = VehicleSpec.model_validate(_read_json(args.spec))
    issues = validate_program(spec, program)
    if not is_valid(issues):
        for issue in issues:
            if issue.severity == "error":
                print(f"error: {issue.message}", file=sys.stderr)
        return 1
    _write_output(to_csv(program), args.output)
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    program_a = _load_program(args.program_a)
    program_b = _load_program(args.program_b)
    result = diff_programs(program_a, program_b)
    _write_output(json.dumps(result, indent=2), args.output)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.cli",
        description="Local, offline batch generation and export for spec2program - no server needed.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_generate = sub.add_parser("generate", help="Generate a program for one VehicleSpec JSON file.")
    p_generate.add_argument("spec", help="Path to a VehicleSpec JSON file.")
    p_generate.add_argument("-o", "--output", help="Write the GenerateResponse JSON here instead of stdout.")
    p_generate.set_defaults(func=cmd_generate)

    p_batch = sub.add_parser("batch", help="Run the pipeline for a JSON array of VehicleSpecs.")
    p_batch.add_argument("specs", help="Path to a JSON file containing an array of VehicleSpec objects.")
    p_batch.add_argument("-o", "--output", help="Write the BatchResponse JSON here instead of stdout.")
    p_batch.set_defaults(func=cmd_batch)

    p_otx = sub.add_parser("export-otx", help="Export a program as OTX-style XML.")
    p_otx.add_argument("program", help="Path to a program JSON file (bare, or a GenerateResponse).")
    p_otx.add_argument("spec", help="Path to the VehicleSpec JSON the program is for.")
    p_otx.add_argument("-o", "--output", help="Write the XML here instead of stdout.")
    p_otx.set_defaults(func=cmd_export_otx)

    p_csv = sub.add_parser("export-csv", help="Export a program as a human-readable work-instruction CSV.")
    p_csv.add_argument("program", help="Path to a program JSON file (bare, or a GenerateResponse).")
    p_csv.add_argument("spec", help="Path to the VehicleSpec JSON the program is for.")
    p_csv.add_argument("-o", "--output", help="Write the CSV here instead of stdout.")
    p_csv.set_defaults(func=cmd_export_csv)

    p_compare = sub.add_parser("compare", help="Diff two programs by step order.")
    p_compare.add_argument("program_a", help="Path to the first program JSON file.")
    p_compare.add_argument("program_b", help="Path to the second program JSON file.")
    p_compare.add_argument("-o", "--output", help="Write the diff JSON here instead of stdout.")
    p_compare.set_defaults(func=cmd_compare)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
