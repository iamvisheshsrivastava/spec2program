"""Runtime corrective actions.

Everything else in this project happens before the vehicle reaches the
line: plan a program, validate it, self-repair it if the LLM got it wrong.
This module handles the other half of "corrective actions" from the JD's
task list - a step that was *executed* and *failed on the actual line*.
That is a different problem: some steps already ran, some ECUs may already
be unlocked, and the fix needs to be a short, targeted retry sequence given
the world as it now is, not a fresh full program.

Same resilience pattern as the rest of the project: try the real LLM first
(it can reason about the specific failure reason in natural language), fall
back to a deterministic rule-based recovery policy if no real provider is
configured or the call fails - so this endpoint never hard-fails either.
"""

from __future__ import annotations

import logging

from .llm_service import MockProvider, generate_recovery_program
from .models import (
    CommissioningProgram,
    CommissioningStep,
    RecoveryRequest,
    RecoveryResponse,
    StepType,
    VehicleSpec,
)
from .validator import is_valid, validate_recovery_steps

logger = logging.getLogger(__name__)


def _already_unlocked(program: CommissioningProgram, failed_order: int) -> set[str]:
    """ECUs that had a completed security-access step before the failure."""
    return {
        s.ecu_id for s in program.steps
        if s.order < failed_order and s.step_type == StepType.SECURITY_ACCESS
    }


def _already_seen_orders(program: CommissioningProgram, failed_order: int) -> set[int]:
    """Step orders that had already run by the time of the failure (inclusive)."""
    return {s.order for s in program.steps if s.order <= failed_order}


def _mock_recovery(
    spec: VehicleSpec, program: CommissioningProgram, failed_step: CommissioningStep,
    failure_reason: str,
) -> list[CommissioningStep]:
    """Deterministic recovery policy: classify the failure reason by keyword,
    prepend whatever precondition it implies is missing, then retry.
    """
    ecu = next((e for e in spec.ecus if e.ecu_id == failed_step.ecu_id), None)
    if ecu is None:
        # Nothing sensible to recover onto - return no steps; the caller's
        # validator will flag the original failed_step reference separately
        # if needed. This keeps the mock policy honest rather than guessing.
        return []

    supports = set(ecu.supported_uds_services)
    reason = failure_reason.lower()
    steps: list[CommissioningStep] = []
    order = len(program.steps)
    prereq: list[int] = []

    def add(step_type: StepType, description: str, uds: str | None, seconds: float) -> None:
        nonlocal order
        order += 1
        steps.append(CommissioningStep(
            order=order, step_type=step_type, ecu_id=ecu.ecu_id, description=description,
            uds_service=uds, estimated_seconds=seconds, depends_on=list(prereq),
        ))
        prereq.clear()
        prereq.append(order)

    if any(k in reason for k in ("security", "unlock", "denied", "access")):
        if "0x27" in supports:
            add(StepType.SECURITY_ACCESS,
                f"Re-establish security access on {ecu.name} after failure ({failure_reason}).",
                "0x27", 4.0)
    elif any(k in reason for k in ("timeout", "communication", "session", "no response", "comm")):
        if "0x10" in supports:
            add(StepType.DIAGNOSTIC_SESSION,
                f"Re-open diagnostic session on {ecu.name} after communication failure ({failure_reason}).",
                "0x10", 3.0)

    # Always retry the action that failed.
    add(failed_step.step_type,
        f"Retry: {failed_step.description}",
        failed_step.uds_service,
        failed_step.estimated_seconds or 5.0)

    # Confirm the retry actually worked, if the ECU supports a read-back.
    if "0x22" in supports:
        add(StepType.VALIDATION,
            f"Validate {ecu.name} after recovery retry.",
            "0x22", 8.0)

    return steps


def generate_recovery(request: RecoveryRequest) -> RecoveryResponse:
    """Produce and validate a corrective sub-program for a runtime failure."""
    spec, program = request.spec, request.program
    failed_step = next(
        (s for s in program.steps if s.order == request.failed_step_order), None
    )
    if failed_step is None:
        raise ValueError(
            f"Step {request.failed_step_order} does not exist in the given program."
        )

    unlocked = _already_unlocked(program, request.failed_step_order)
    seen = _already_seen_orders(program, request.failed_step_order)

    notes: str | None = None
    try:
        raw, provider_name = generate_recovery_program(
            spec, program, failed_step, request.failure_reason
        )
        recovery_steps = [CommissioningStep.model_validate(s) for s in raw.get("steps", [])]
        notes = raw.get("notes")
    except Exception as exc:  # noqa: BLE001 - any failure falls back the same way
        logger.warning("Recovery generation failed, falling back to mock policy: %s", exc)
        recovery_steps = _mock_recovery(spec, program, failed_step, request.failure_reason)
        provider_name = "mock-fallback"
        notes = (
            f"Fell back to the deterministic recovery policy because the LLM call failed "
            f"({exc.__class__.__name__}: {exc})."
        )

    issues = validate_recovery_steps(
        spec, recovery_steps, already_unlocked=unlocked, already_seen_orders=seen
    )

    return RecoveryResponse(
        recovery_steps=recovery_steps,
        notes=notes,
        provider=provider_name,
        is_valid=is_valid(issues),
        validation=issues,
    )
