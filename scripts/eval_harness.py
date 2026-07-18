"""Evaluation harness: does the self-repair loop actually help?

Generates a set of randomised (but structurally realistic) vehicle specs and
runs them through the exact same ``run_pipeline`` the API uses, so the
numbers reported here describe production behaviour, not a simplified
re-implementation. It measures, per provider:

- Validity rate on the *first* generation attempt (no self-repair).
- Validity rate *after* the self-repair loop (up to
  ``generator.MAX_REPAIR_ATTEMPTS`` rounds).

The mock planner is deterministic and rule-based by construction, so it is
included mainly as a 100%-valid baseline. The interesting numbers come from
running a real LLM with ``--live`` (uses whatever provider/key is configured
in your environment/`.env`). Live runs cost real API calls, so ``--live-n``
defaults small.

Usage:
    python scripts/eval_harness.py --n 20                 # mock only, offline, free
    python scripts/eval_harness.py --n 20 --live --live-n 5
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import settings  # noqa: E402
from backend.generator import MAX_REPAIR_ATTEMPTS, run_pipeline  # noqa: E402
from backend.llm_service import MockProvider, get_provider  # noqa: E402
from backend.models import Ecu, VehicleSpec  # noqa: E402

ECU_POOL = [
    ("BMS", "Battery Management System"),
    ("GATEWAY", "Central Gateway"),
    ("IC", "Instrument Cluster"),
    ("INFOTAINMENT", "Infotainment Head Unit"),
    ("ADAS", "ADAS Domain Controller"),
    ("BCM", "Body Control Module"),
]
UDS_POOL = ["0x10", "0x27", "0x34", "0x36", "0x2E", "0x22", "0x31", "0x14"]


def random_spec(rng: random.Random, idx: int) -> VehicleSpec:
    """Build one randomised-but-plausible vehicle spec."""
    n_ecus = rng.randint(2, 5)
    chosen = rng.sample(ECU_POOL, n_ecus)
    ecus: list[Ecu] = []
    for ecu_id, name in chosen:
        supported = sorted(rng.sample(UDS_POOL, rng.randint(3, len(UDS_POOL))))
        current_version = f"V{rng.randint(1, 20)}"
        needs_update = rng.random() < 0.6
        target_version = f"V{rng.randint(1, 20)}" if needs_update else current_version
        ecus.append(
            Ecu(
                ecu_id=ecu_id,
                name=name,
                part_number=f"PN-{ecu_id}-{idx}",
                software_version=current_version,
                target_software_version=target_version,
                supported_uds_services=supported,
            )
        )
    return VehicleSpec(
        vehicle_id=f"EVAL-{idx:04d}",
        model="EvalModel",
        model_year=2026,
        configuration={"drivetrain": rng.choice(["BEV", "ICE", "PHEV"])},
        process_standards=["Security access required before flashing."],
        ecus=ecus,
    )


def eval_provider(provider, specs: list[VehicleSpec], max_repair_attempts: int) -> tuple[int, int]:
    """Return (valid_count, total) for a provider over a set of specs."""
    valid = 0
    for spec in specs:
        result = run_pipeline(spec, provider, max_repair_attempts=max_repair_attempts)
        if result.is_valid:
            valid += 1
    return valid, len(specs)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=20, help="Number of specs for the offline mock eval.")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed, for reproducible specs.")
    parser.add_argument("--live", action="store_true", help="Also evaluate the real configured LLM provider.")
    parser.add_argument("--live-n", type=int, default=5, help="Number of specs for the live LLM eval (costs API calls).")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    specs = [random_spec(rng, i) for i in range(args.n)]

    lines = [
        "# spec2program evaluation report",
        "",
        f"Seed: `{args.seed}` · Specs: `{args.n}` · Generated: reproducible via this seed.",
        "",
        "| Provider | Mode | Valid / Total | Validity rate |",
        "|---|---|---|---|",
    ]

    mock_valid, mock_total = eval_provider(MockProvider(), specs, max_repair_attempts=0)
    lines.append(
        f"| mock | deterministic baseline | {mock_valid}/{mock_total} | {mock_valid / mock_total:.0%} |"
    )
    print(f"mock: {mock_valid}/{mock_total} valid ({mock_valid / mock_total:.0%})")

    if args.live:
        if not settings.llm_api_key:
            print("No LLM_API_KEY configured — skipping live eval. Set LLM_PROVIDER/LLM_API_KEY in .env.")
        else:
            live_specs = specs[: args.live_n]
            provider = get_provider()

            first_valid, total = eval_provider(provider, live_specs, max_repair_attempts=0)
            lines.append(
                f"| {provider.name} | first attempt, no repair | {first_valid}/{total} | {first_valid / total:.0%} |"
            )
            print(f"{provider.name} (no repair): {first_valid}/{total} valid ({first_valid / total:.0%})")

            repaired_valid, _ = eval_provider(provider, live_specs, max_repair_attempts=MAX_REPAIR_ATTEMPTS)
            lines.append(
                f"| {provider.name} | with self-repair (up to {MAX_REPAIR_ATTEMPTS} rounds) "
                f"| {repaired_valid}/{total} | {repaired_valid / total:.0%} |"
            )
            print(
                f"{provider.name} (up to {MAX_REPAIR_ATTEMPTS} repair rounds): "
                f"{repaired_valid}/{total} valid ({repaired_valid / total:.0%})"
            )

            delta = repaired_valid - first_valid
            lines.append("")
            lines.append(
                f"Self-repair improved validity by **{delta}/{total} spec(s)** "
                f"({delta / total:+.0%}) on this run."
                if total
                else ""
            )
    else:
        lines.append("")
        lines.append("(Run with `--live` to also evaluate the real LLM provider and the self-repair loop.)")

    report = "\n".join(lines) + "\n"
    out_path = ROOT / "data" / "eval_report.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"\nReport written to {out_path}")


if __name__ == "__main__":
    main()
