"""Diff/compare two commissioning programs.

Issue #20: when a program changes - a re-generation after a spec edit, a
repair-loop result vs. the first attempt, or two programs for related
vehicles - there was no way to see *what* changed without diffing raw JSON
by eye. This module compares two ``CommissioningProgram``s step-by-step,
matched by ``order``, and reports what was added, removed, or modified, and
which fields changed on a modified step.
"""

from __future__ import annotations

from .models import CommissioningProgram, CommissioningStep

# Fields compared when deciding whether a step (matched by `order`) changed.
_COMPARED_FIELDS = (
    "step_type", "ecu_id", "description", "uds_service", "estimated_seconds", "depends_on",
)


def _field_value(step: CommissioningStep, field: str):
    value = getattr(step, field)
    # StepType is an Enum; compare/report its plain string value.
    return value.value if hasattr(value, "value") else value


def diff_programs(program_a: CommissioningProgram, program_b: CommissioningProgram) -> dict:
    """Compare two programs by step order and return a structured diff.

    Returns a plain dict (rather than a pydantic model) of the shape:
        {
          "vehicle_id_a": str, "vehicle_id_b": str,
          "added": [step order ints present only in program_b],
          "removed": [step order ints present only in program_a],
          "modified": [{"order": int, "changes": {field: {"before": ..., "after": ...}}}],
          "unchanged_count": int,
        }
    """
    by_order_a = {s.order: s for s in program_a.steps}
    by_order_b = {s.order: s for s in program_b.steps}

    orders_a, orders_b = set(by_order_a), set(by_order_b)
    added = sorted(orders_b - orders_a)
    removed = sorted(orders_a - orders_b)
    common = sorted(orders_a & orders_b)

    modified: list[dict] = []
    unchanged_count = 0
    for order in common:
        step_a, step_b = by_order_a[order], by_order_b[order]
        changes = {}
        for field in _COMPARED_FIELDS:
            before, after = _field_value(step_a, field), _field_value(step_b, field)
            if before != after:
                changes[field] = {"before": before, "after": after}
        if changes:
            modified.append({"order": order, "changes": changes})
        else:
            unchanged_count += 1

    return {
        "vehicle_id_a": program_a.vehicle_id,
        "vehicle_id_b": program_b.vehicle_id,
        "added": added,
        "removed": removed,
        "modified": modified,
        "unchanged_count": unchanged_count,
    }
