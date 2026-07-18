"""Generation pipeline.

Ties the pieces together: take a ``VehicleSpec``, ask the configured LLM
provider for a program, self-repair it against the validator if it fails,
apply the learned duration model, and compute analytics + critical-path
optimisation. This is the single orchestration point the API calls
(``/api/generate`` and ``/api/batch``) and that the evaluation harness reuses,
which keeps the route handlers in ``main.py`` thin and keeps eval numbers
honest (the harness runs the exact same code path as production).

Resilience: a real LLM call can fail for many reasons outside our control
(network hiccup, rate limit, the model returning malformed JSON). Rather than
surfacing a hard 500 to the user, this pipeline falls back to the offline mock
planner and says so plainly in the response - never a silent, hidden failure.

Self-repair: a real LLM call can also *succeed* but still produce a program
that fails rule-based validation (wrong ECU reference, missing security
access, etc). Rather than showing that first attempt as-is, the pipeline
feeds the validator's own findings back to the model and gives it a bounded
number of chances to fix its own output before accepting the result.
"""

from __future__ import annotations

import logging

from .analytics import analyse_program
from .duration_model import is_available as duration_model_available
from .duration_model import predict_seconds
from .llm_service import LLMProvider, MockProvider, get_provider
from .models import (
    CommissioningProgram,
    GenerateResponse,
    VehicleSpec,
)
from .scheduler import compute_optimization
from .validator import is_valid, validate_program

logger = logging.getLogger(__name__)

MAX_REPAIR_ATTEMPTS = 2


def _apply_learned_durations(spec: VehicleSpec, program: CommissioningProgram) -> bool:
    """Overwrite each step's estimated_seconds using the trained duration model.

    Returns True if the model was applied (so the caller can note it), False
    if no trained model file is present (durations are left as the generator
    - LLM or mock - originally estimated them).
    """
    if not duration_model_available():
        return False

    ecu_by_id = {ecu.ecu_id: ecu for ecu in spec.ecus}
    for step in program.steps:
        ecu = ecu_by_id.get(step.ecu_id)
        flash_size_proxy = 0.0
        if ecu and ecu.target_software_version:
            flash_size_proxy = float(len(ecu.target_software_version))
        predicted = predict_seconds(step.step_type.value, flash_size_proxy)
        if predicted is not None:
            step.estimated_seconds = round(predicted, 1)
    return True


def run_pipeline(
    spec: VehicleSpec,
    provider: LLMProvider,
    *,
    max_repair_attempts: int = MAX_REPAIR_ATTEMPTS,
) -> GenerateResponse:
    """Run spec -> program -> (self-repair) -> durations -> validation -> analytics.

    Exposed separately from ``generate()`` so both the API route and the
    evaluation harness (``scripts/eval_harness.py``) exercise the exact same
    logic against an explicitly chosen provider, rather than the harness
    re-implementing (and potentially drifting from) production behaviour.
    """
    fallback_note: str | None = None
    repair_attempts = 0

    try:
        raw = provider.generate_program(spec)
        program = CommissioningProgram.model_validate(raw)
        provider_name = provider.name
    except Exception as exc:  # noqa: BLE001 - any provider failure is handled the same way
        logger.warning("Provider '%s' failed, falling back to mock: %s", provider.name, exc)
        fallback_note = (
            f"Fell back to the offline mock planner because the '{provider.name}' "
            f"provider failed ({exc.__class__.__name__}: {exc})."
        )
        raw = MockProvider().generate_program(spec)
        program = CommissioningProgram.model_validate(raw)
        provider_name = "mock-fallback"

    issues = validate_program(spec, program)

    # Self-repair loop: only meaningful for real providers that failed
    # validation (the mock planner is deterministic and already schema/rule
    # valid by construction, and a provider that just fell back to mock has
    # nothing further to repair).
    repair_note: str | None = None
    if (
        not is_valid(issues)
        and hasattr(provider, "repair_program")
        and provider_name not in ("mock", "mock-fallback")
    ):
        current_raw = raw
        while not is_valid(issues) and repair_attempts < max_repair_attempts:
            repair_attempts += 1
            try:
                current_raw = provider.repair_program(spec, current_raw, issues)
                candidate = CommissioningProgram.model_validate(current_raw)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Repair attempt %d for provider '%s' failed: %s",
                    repair_attempts, provider.name, exc,
                )
                break
            program = candidate
            issues = validate_program(spec, program)
        if repair_attempts:
            outcome = "resolved all issues" if is_valid(issues) else "did not resolve every issue"
            repair_note = (
                f"Self-repair loop ran {repair_attempts} round(s) against the "
                f"'{provider_name}' provider and {outcome}."
            )

    notes = [n for n in (program.notes, fallback_note, repair_note) if n]
    program.notes = " ".join(notes) if notes else None

    # Apply the learned duration model (if trained) before computing analytics
    # and the critical-path schedule, so every downstream number reflects
    # data-driven estimates rather than the hardcoded TIME_BUDGET table.
    if _apply_learned_durations(spec, program):
        durations_note = "Step durations estimated with a regression model trained on run-log data."
        program.notes = f"{program.notes} {durations_note}" if program.notes else durations_note

    analytics = analyse_program(spec, program)
    optimization = compute_optimization(program)

    return GenerateResponse(
        program=program,
        validation=issues,
        analytics=analytics,
        optimization=optimization,
        is_valid=is_valid(issues),
        provider=provider_name,
        repair_attempts=repair_attempts,
    )


def generate(spec: VehicleSpec) -> GenerateResponse:
    """Run the full pipeline using whichever provider is configured."""
    return run_pipeline(spec, get_provider())
