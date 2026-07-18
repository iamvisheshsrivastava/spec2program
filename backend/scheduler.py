"""Critical-path scheduling.

``ProgramAnalytics.estimated_cycle_time_seconds`` reports the naive sequential
total: every step run back-to-back. That is not the real optimisation
question. A commissioning station typically has several communication
channels (or several stations), so independent steps - ones that do not
depend on each other - can run concurrently. The real lower bound on cycle
time is the length of the *critical path*: the longest chain of steps that
must run one after another because of explicit dependencies.

This module computes that lower bound with a standard earliest-start-time
forward pass over the step dependency graph (the same idea as CPM / PERT
scheduling used in project management), and reconstructs which steps sit on
the critical path so the UI can highlight exactly where the bottleneck is.
"""

from __future__ import annotations

from .models import CommissioningProgram, OptimizationResult, ScheduledStep


def compute_optimization(program: CommissioningProgram) -> OptimizationResult:
    """Compute the critical-path schedule and speedup vs. the sequential plan."""

    steps = program.steps
    if not steps:
        return OptimizationResult(
            sequential_seconds=0.0,
            critical_path_seconds=0.0,
            speedup_factor=1.0,
            critical_path_steps=[],
            schedule=[],
        )

    steps_by_order = {s.order: s for s in steps}
    sequential_seconds = sum(s.estimated_seconds for s in steps)

    # Forward pass: earliest_start[order] = the max end time of all of its
    # prerequisites (0 if it has none). Steps are processed in declared
    # 'order' - the program is already meant to be a valid topological order,
    # and the validator flags it if a dependency points forward, so this
    # simple single-pass approach is safe for well-formed programs.
    earliest: dict[int, tuple[float, float]] = {}  # order -> (start, end)
    predecessor_on_path: dict[int, int | None] = {}

    for step in sorted(steps, key=lambda s: s.order):
        best_start = 0.0
        best_pred: int | None = None
        for dep in step.depends_on:
            if dep in earliest:
                dep_end = earliest[dep][1]
                if dep_end > best_start:
                    best_start = dep_end
                    best_pred = dep
        end = best_start + step.estimated_seconds
        earliest[step.order] = (best_start, end)
        predecessor_on_path[step.order] = best_pred

    critical_path_seconds = max(end for _, end in earliest.values())

    # Reconstruct the critical path by walking backward from whichever step
    # finishes last (ties broken by highest order, i.e. the last one planned).
    finishing_order = max(
        earliest, key=lambda o: (earliest[o][1], o)
    )
    path: list[int] = []
    cursor: int | None = finishing_order
    while cursor is not None:
        path.append(cursor)
        cursor = predecessor_on_path.get(cursor)
    path.reverse()

    speedup = (
        round(sequential_seconds / critical_path_seconds, 2)
        if critical_path_seconds > 0
        else 1.0
    )

    schedule = [
        ScheduledStep(order=order, start=round(start, 1), end=round(end, 1))
        for order, (start, end) in sorted(earliest.items())
    ]

    return OptimizationResult(
        sequential_seconds=round(sequential_seconds, 1),
        critical_path_seconds=round(critical_path_seconds, 1),
        speedup_factor=speedup,
        critical_path_steps=path,
        schedule=schedule,
    )
