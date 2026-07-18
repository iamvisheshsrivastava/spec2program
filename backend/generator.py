"""Generation pipeline.

Ties the pieces together: take a ``VehicleSpec``, ask the configured LLM
provider for a program, parse and validate the result, and compute analytics.
This is the single orchestration point the API calls, which keeps the route
handlers in ``main.py`` thin.

Resilience: a real LLM call can fail for many reasons outside our control
(network hiccup, rate limit, the model returning malformed JSON). Rather than
surfacing a hard 500 to the user, this pipeline falls back to the offline mock
planner and says so plainly in the response - never a silent, hidden failure.
"""

from __future__ import annotations

import logging

from .analytics import analyse_program
from .llm_service import MockProvider, get_provider
from .models import (
    CommissioningProgram,
    GenerateResponse,
    VehicleSpec,
)
from .validator import is_valid, validate_program

logger = logging.getLogger(__name__)


def generate(spec: VehicleSpec) -> GenerateResponse:
    """Run the full spec -> program -> validation -> analytics pipeline."""

    # 1) Choose the provider (mock, or a real OpenAI-compatible model).
    provider = get_provider()
    fallback_note: str | None = None

    # 2) Ask it for a raw program dict, and parse it into a typed model.
    #    Any failure here (network error, bad JSON, schema mismatch) triggers
    #    a graceful, visible fallback to the deterministic mock planner - the
    #    product must never hard-fail in front of a user.
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

    if fallback_note:
        program.notes = f"{program.notes} {fallback_note}" if program.notes else fallback_note

    # 3) Validate the program against the spec (rule-based checks).
    issues = validate_program(spec, program)

    # 4) Compute optimisation-focused analytics.
    analytics = analyse_program(spec, program)

    return GenerateResponse(
        program=program,
        validation=issues,
        analytics=analytics,
        is_valid=is_valid(issues),
        provider=provider_name,
    )
