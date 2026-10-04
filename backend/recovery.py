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
    ValidationIssue,
    VehicleSpec,
)
from .validator import check_notes_for_unverified_claims, is_valid, validate_recovery_steps

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
        # Nothing sensible to recover onto. Previously this returned an
        # empty list silently (issue #9): zero recovery steps meant zero
        # issues from validate_recovery_steps(), and is_valid([]) is
        # vacuously True - a green "recovery passed" response for a fix
        # that was never generated. generate_recovery() below now also
        # treats an empty recovery_steps list as a hard error regardless of
        # cause, but this keeps the mock policy's own reasoning honest: the
        # ECU/spec mismatch is itself a finding, not a no-op.
        raise ValueError(
            f"Failed step {failed_step.order} targets ECU '{failed_step.ecu_id}', "
            f"which is not in the vehicle specification - no recovery can be planned."
        )

    supports = set(ecu.supported_uds_services)
    reason = failure_reason.lower()
    steps: list[CommissioningStep] = []
    # Use the highest existing order, not the step count, so recovery steps
    # never collide with an existing order when the program has gaps (e.g.
    # orders 1, 3, 5 after earlier edits or partial recovery rounds).
    order = max((s.order for s in program.steps), default=0)
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
        if not recovery_steps:
            # Issue #9: a model that returns {"steps": []} (or omits "steps")
            # does not raise, so this used to skip the mock fallback entirely
            # and return an empty-but-"valid" response. Treat it the same as
            # any other provider failure so the deterministic policy gets a
            # real chance to produce something.
            raise ValueError(f"'{provider_name}' provider returned zero recovery steps.")
        notes = raw.get("notes")
    except Exception as exc:  # noqa: BLE001 - any failure falls back the same way
        logger.warning("Recovery generation failed, falling back to mock policy: %s", exc)
        try:
            recovery_steps = _mock_recovery(spec, program, failed_step, request.failure_reason)
        except ValueError as mock_exc:
            # The deterministic policy also had nothing sensible to do (e.g.
            # the failed step's ECU is not in the spec at all). Surface that
            # as the finding it is rather than silently producing no steps.
            recovery_steps = []
            notes = str(mock_exc)
            provider_name = "mock-fallback"
        else:
            provider_name = "mock-fallback"
            notes = (
                f"Fell back to the deterministic recovery policy because the LLM call failed "
                f"({exc.__class__.__name__}: {exc})."
            )

    issues = validate_recovery_steps(
        spec, recovery_steps, already_unlocked=unlocked, already_seen_orders=seen
    )
    if not recovery_steps:
        # Issue #9: zero recovery steps means validate_recovery_steps() ran
        # zero iterations and found zero issues, and is_valid([]) is
        # vacuously True - a "passed validation" response for a fix that
        # does not exist. An empty recovery is always a failure to report,
        # never a silent pass.
        issues.append(ValidationIssue(
            severity="error",
            message=(
                "No corrective recovery steps could be generated for this failure. "
                "This is not a validated recovery - treat it as recovery failing, not succeeding."
            ),
        ))
    issues.extend(check_notes_for_unverified_claims(notes))

    return RecoveryResponse(
        recovery_steps=recovery_steps,
        notes=notes,
        provider=provider_name,
        is_valid=is_valid(issues),
        validation=issues,
    )
