"""Rule-based validator.

An LLM (or any generator) can produce a plausible-looking program that is
subtly wrong: referencing an ECU that is not in the vehicle, using a UDS
service the ECU does not support, or flashing before unlocking security
access. In a production plant those mistakes cost cycle time and stability.

This module checks a generated ``CommissioningProgram`` against the
``VehicleSpec`` it was generated from and returns a list of issues. It is the
"intellectual honesty" layer of the system: we never trust the generator's
output blindly, we verify it.
"""

from __future__ import annotations

from .models import (
    CommissioningProgram,
    StepType,
    ValidationIssue,
    VehicleSpec,
)


def validate_program(
    spec: VehicleSpec, program: CommissioningProgram
) -> list[ValidationIssue]:
    """Return all validation issues (errors and warnings) for a program."""
    issues: list[ValidationIssue] = []

    # Index the spec for quick lookups.
    ecu_by_id = {ecu.ecu_id: ecu for ecu in spec.ecus}

    # ---- Structural checks --------------------------------------------------
    # Vehicle id must match the spec.
    if program.vehicle_id != spec.vehicle_id:
        issues.append(ValidationIssue(
            severity="error",
            message=(f"Program vehicle_id '{program.vehicle_id}' does not match "
                     f"spec vehicle_id '{spec.vehicle_id}'."),
        ))

    if not program.steps:
        issues.append(ValidationIssue(
            severity="error", message="Program contains no steps."))
        return issues  # nothing further to check

    # Step ordering should be a contiguous 1..N sequence.
    orders = [s.order for s in program.steps]
    if sorted(orders) != list(range(1, len(orders) + 1)):
        issues.append(ValidationIssue(
            severity="warning",
            message="Step 'order' values are not a contiguous 1..N sequence.",
        ))

    seen_orders: set[int] = set()

    # Track, per ECU, whether security access has been granted so far. Used to
    # enforce the safety rule that flashing/writing requires a prior unlock.
    unlocked: set[str] = set()

    for step in program.steps:
        # ---- Reference checks ----------------------------------------------
        ecu = ecu_by_id.get(step.ecu_id)
        if ecu is None:
            issues.append(ValidationIssue(
                severity="error",
                step_order=step.order,
                message=(f"Step {step.order} targets ECU '{step.ecu_id}', which "
                         f"is not in the vehicle specification."),
            ))
            # Can't check UDS support without a known ECU.
            seen_orders.add(step.order)
            continue

        # ---- UDS support checks --------------------------------------------
        if step.uds_service and step.uds_service not in ecu.supported_uds_services:
            issues.append(ValidationIssue(
                severity="error",
                step_order=step.order,
                message=(f"Step {step.order} uses UDS service {step.uds_service} on "
                         f"'{ecu.ecu_id}', but that ECU does not support it."),
            ))

        # ---- Safety-ordering checks ----------------------------------------
        if step.step_type == StepType.SECURITY_ACCESS:
            unlocked.add(step.ecu_id)

        if step.step_type in (StepType.FLASH_SOFTWARE, StepType.WRITE_PARAMETER):
            if step.ecu_id not in unlocked and "0x27" in ecu.supported_uds_services:
                issues.append(ValidationIssue(
                    severity="error",
                    step_order=step.order,
                    message=(f"Step {step.order} ({step.step_type.value}) on "
                             f"'{ecu.ecu_id}' runs before a security-access step."),
                ))

        # ---- Dependency checks ---------------------------------------------
        for dep in step.depends_on:
            if dep not in seen_orders:
                issues.append(ValidationIssue(
                    severity="error",
                    step_order=step.order,
                    message=(f"Step {step.order} depends on step {dep}, which does "
                             f"not appear earlier in the program."),
                ))

        seen_orders.add(step.order)

    # ---- Coverage checks ----------------------------------------------------
    # Every ECU that needs a software update should actually be flashed.
    flashed = {s.ecu_id for s in program.steps if s.step_type == StepType.FLASH_SOFTWARE}
    for ecu in spec.ecus:
        needs_flash = (
            ecu.target_software_version is not None
            and ecu.target_software_version != ecu.software_version
        )
        if needs_flash and ecu.ecu_id not in flashed:
            issues.append(ValidationIssue(
                severity="warning",
                message=(f"ECU '{ecu.ecu_id}' needs a software update "
                         f"({ecu.software_version} -> {ecu.target_software_version}) "
                         f"but the program never flashes it."),
            ))

    # A well-formed program should end by clearing faults and validating.
    types_present = {s.step_type for s in program.steps}
    if StepType.FAULT_CLEAR not in types_present:
        issues.append(ValidationIssue(
            severity="warning",
            message="Program never clears diagnostic trouble codes (UDS 0x14).",
        ))
    if StepType.VALIDATION not in types_present:
        issues.append(ValidationIssue(
            severity="warning",
            message="Program contains no validation step.",
        ))

    return issues


def is_valid(issues: list[ValidationIssue]) -> bool:
    """A program is 'valid' if it has no error-severity issues."""
    return not any(issue.severity == "error" for issue in issues)
