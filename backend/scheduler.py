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

Issue #7: a step's ``depends_on`` is client/LLM-controlled data, not a
guaranteed-valid DAG. A forward reference (a step depending on a later
order) or a cycle must not be silently dropped - that produces confidently
wrong numbers (too-low critical path, too-high speedup) instead of an error.
``_topological_order`` below makes that explicit: it raises ``ValueError``
for a dangling or cyclic dependency, and both scheduling functions walk
steps in the resulting topological order rather than trusting declared
``order`` to already be one.
"""

from __future__ import annotations

import heapq

from .models import (
    ChannelScheduledStep,
    ChannelScheduleResult,
    ChannelSweepPoint,
    ChannelSweepResult,
    CommissioningProgram,
    CommissioningStep,
    OptimizationResult,
    ScheduledStep,
)


def _topological_order(steps: list[CommissioningStep]) -> list[CommissioningStep]:
    """Return ``steps`` in a valid topological order of their ``depends_on`` edges.

    Raises ``ValueError`` if a step depends on an order that does not exist
    in ``steps``, or if the dependency graph contains a cycle (including a
    step that depends on a later-declared order in a way that can never be
    satisfied). Kahn's algorithm, with ties broken by ascending ``order`` so
    a well-formed program (already a valid 1..N sequence with only backward
    dependencies) sorts identically to before.
    """
    by_order = {s.order: s for s in steps}
    if len(by_order) != len(steps):
        raise ValueError("Duplicate step 'order' values in program.")

    # dependents[d] = steps that list d as a prerequisite.
    dependents: dict[int, list[int]] = {o: [] for o in by_order}
    in_degree: dict[int, int] = {o: 0 for o in by_order}

    for step in steps:
        for dep in step.depends_on:
            if dep not in by_order:
                raise ValueError(
                    f"Step {step.order} depends on step {dep}, which does not exist "
                    f"in this program."
                )
            dependents[dep].append(step.order)
            in_degree[step.order] += 1

    ready = [o for o, deg in in_degree.items() if deg == 0]
    heapq.heapify(ready)

    ordered: list[CommissioningStep] = []
    while ready:
        order = heapq.heappop(ready)
        ordered.append(by_order[order])
        for nxt in dependents[order]:
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                heapq.heappush(ready, nxt)

    if len(ordered) != len(steps):
        remaining = sorted(set(by_order) - {s.order for s in ordered})
        raise ValueError(
            f"Program's step dependencies form a cycle (unreachable orders: {remaining})."
        )
    return ordered


def compute_optimization(program: CommissioningProgram) -> OptimizationResult:
    """Compute the critical-path schedule and speedup vs. the sequential plan.

    Raises ``ValueError`` if ``program.steps`` has a dangling or cyclic
    dependency (issue #7) - callers that accept client-supplied programs
    should catch this and report it explicitly rather than trusting the
    numbers that would otherwise come out.
    """

    steps = program.steps
    if not steps:
        return OptimizationResult(
            sequential_seconds=0.0,
            critical_path_seconds=0.0,
            speedup_factor=1.0,
            critical_path_steps=[],
            schedule=[],
        )

    ordered_steps = _topological_order(steps)
    sequential_seconds = sum(s.estimated_seconds for s in steps)

    # Forward pass over the topological order: earliest_start[order] = the
    # max end time of all of its prerequisites (0 if it has none).
    earliest: dict[int, tuple[float, float]] = {}  # order -> (start, end)
    predecessor_on_path: dict[int, int | None] = {}

    for step in ordered_steps:
        best_start = 0.0
        best_pred: int | None = None
        for dep in step.depends_on:
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


def schedule_with_channels(program: CommissioningProgram, channels: int) -> ChannelScheduleResult:
    """Schedule a program under a finite number of parallel tester channels.

    ``compute_optimization`` above answers "what's the theoretical minimum if
    we had unlimited parallel hardware" - useful as a ceiling, but not
    actionable on its own: a real commissioning station has a specific,
    finite number of communication channels. This answers the practical
    question instead: given exactly N channels, what's the actual cycle time,
    and which step runs on which channel?

    This is resource-constrained project scheduling (RCPS), which is
    NP-hard in general. We use the standard, well-established heuristic:
    greedy list scheduling - at each step (processed in topological order,
    so a forward/backward dependency is always honoured rather than
    silently skipped, see issue #7), assign it to whichever free-or-soon
    channel actually gives it the earliest start time, breaking ties toward
    the *busiest* channel that still meets that start time (issue #6) so an
    idle channel is preserved for whatever becomes ready next rather than
    being burned on a step that was going to start at the same time anyway.

    Raises ``ValueError`` if ``channels < 1`` or the dependency graph is not
    a DAG (see ``_topological_order``).
    """
    if channels < 1:
        raise ValueError(f"channels must be >= 1, got {channels}.")

    steps = program.steps
    if not steps:
        return ChannelScheduleResult(channels=channels, cycle_time_seconds=0.0, schedule=[])

    ordered_steps = _topological_order(steps)

    finished_end: dict[int, float] = {}
    placed: dict[int, ChannelScheduledStep] = {}
    channel_free_at = [0.0] * channels

    for step in ordered_steps:
        dep_end = 0.0
        for dep in step.depends_on:
            dep_end = max(dep_end, finished_end[dep])

        # Pick the channel that gives the earliest actual start time; among
        # channels tied on that start time, prefer the busiest one (largest
        # free_at) so the idlest channel stays available for later work.
        channel_idx = min(
            range(channels),
            key=lambda c: (max(dep_end, channel_free_at[c]), -channel_free_at[c]),
        )
        start = max(dep_end, channel_free_at[channel_idx])
        end = start + step.estimated_seconds
        channel_free_at[channel_idx] = end

        finished_end[step.order] = end
        placed[step.order] = ChannelScheduledStep(
            order=step.order, channel=channel_idx, start=round(start, 1), end=round(end, 1)
        )

    cycle_time = max(finished_end.values()) if finished_end else 0.0

    return ChannelScheduleResult(
        channels=channels,
        cycle_time_seconds=round(cycle_time, 1),
        schedule=[placed[s.order] for s in sorted(steps, key=lambda s: s.order)],
    )


def _cycle_time_for_channels(ordered_steps: list[CommissioningStep], channels: int) -> float:
    """Cycle time alone for a given channel count, skipping per-step objects.

    Same greedy algorithm as ``schedule_with_channels``, but it does not
    build a ``ChannelScheduledStep`` for every step. The sweep below only
    ever reads the cycle time, and constructing (and validating) one Pydantic
    model per step per channel count dominated the sweep's cost.

    ``ordered_steps`` must already be in a valid topological order (callers
    compute it once via ``_topological_order`` and reuse it for every
    channel count in the sweep).
    """
    finished_end: dict[int, float] = {}
    channel_free_at = [0.0] * channels
    makespan = 0.0

    for step in ordered_steps:
        dep_end = 0.0
        for dep in step.depends_on:
            dep_end = max(dep_end, finished_end[dep])

        channel_idx = min(
            range(channels),
            key=lambda c: (max(dep_end, channel_free_at[c]), -channel_free_at[c]),
        )
        end = max(dep_end, channel_free_at[channel_idx]) + step.estimated_seconds
        channel_free_at[channel_idx] = end

        finished_end[step.order] = end
        if end > makespan:
            makespan = end

    return makespan


def channel_sweep(program: CommissioningProgram, max_channels: int) -> ChannelSweepResult:
    """Cycle time for every channel count from 1 to max_channels.

    Surfaces the diminishing-returns curve directly: past a certain number
    of channels, adding more stops helping because the critical path (not
    channel contention) becomes the bottleneck - the same ceiling
    ``compute_optimization`` computes for unlimited channels.

    Raises ``ValueError`` if ``max_channels < 1`` or the dependency graph is
    not a DAG.
    """
    if max_channels < 1:
        raise ValueError(f"max_channels must be >= 1, got {max_channels}.")

    steps = program.steps
    if not steps:
        return ChannelSweepResult(
            points=[
                ChannelSweepPoint(channels=n, cycle_time_seconds=0.0)
                for n in range(1, max_channels + 1)
            ]
        )

    ordered_steps = _topological_order(steps)

    # The critical path is a hard floor on cycle time: no number of channels
    # can beat it. Once the sweep reaches that floor, every remaining point
    # is identical, so we stop scheduling and fill the tail in directly
    # rather than re-running the greedy pass for each larger channel count.
    floor = compute_optimization(program).critical_path_seconds

    points: list[ChannelSweepPoint] = []
    for n in range(1, max_channels + 1):
        cycle = round(_cycle_time_for_channels(ordered_steps, n), 1)
        points.append(ChannelSweepPoint(channels=n, cycle_time_seconds=cycle))
        if cycle <= floor:
            points.extend(
                ChannelSweepPoint(channels=m, cycle_time_seconds=cycle)
                for m in range(n + 1, max_channels + 1)
            )
            break

    return ChannelSweepResult(points=points)
