"""Batch mode: run several vehicle specs through the pipeline and aggregate.

A single spec's optimisation numbers are interesting; a fleet's are what a
process engineer actually acts on. This module runs ``run_pipeline`` once per
spec and rolls the results up into fleet-level findings: overall validity
rate, average cycle time and speedup, which ECUs most often dominate a
vehicle's program (a proxy for where to focus commissioning-process
improvement work), and the most frequently recurring validation issue.
"""

from __future__ import annotations

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from .config import settings
from .generator import generate
from .models import BatchAggregate, BatchResponse, VehicleSpec

# Issue #26: a brand-new ``ThreadPoolExecutor`` used to be created (and torn
# down) inside every ``run_batch`` call. Each request's handler thread (one
# of FastAPI/Starlette's own, already-bounded pool of worker threads) would
# then block on ``pool.map`` while spinning up to ``batch_max_workers`` more
# OS threads of its own. That is fine for one request at a time, but several
# concurrent ``/api/batch`` calls each add their own full-sized pool on top
# of the one they are already occupying, so thread usage compounds with the
# number of in-flight batch requests instead of staying bounded - a
# thread-exhaustion risk under concurrent load.
#
# A full async rewrite of the LLM call path (``httpx.AsyncClient`` plus
# ``async def`` routes/generator/llm_service) would remove the thread pool
# entirely, but it touches every provider class in ``llm_service.py`` and
# every caller of ``generate()``/``run_batch()`` (the FastAPI routes, the
# offline CLI, and the whole test suite), which is a lot of surface area to
# change for a security/robustness fix. Reusing a single, lazily-created,
# process-wide pool - sized once, from the same ``batch_max_workers`` setting
# - keeps the existing synchronous pipeline untouched while capping total
# batch-fan-out threads at ``batch_max_workers`` no matter how many requests
# are in flight at once, which directly addresses the compounding/exhaustion
# risk with a much smaller, lower-risk change.
_executor_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            workers = max(1, settings.batch_max_workers)
            _executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="batch")
        return _executor


def shutdown_executor() -> None:
    """Release the shared batch thread pool. Call this on application shutdown."""
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False)
            _executor = None


def _generate_all(specs: list[VehicleSpec]) -> list:
    """Run ``generate`` for every spec, overlapping the LLM round-trips.

    Each spec's pipeline is dominated by network latency (one LLM call, plus
    up to two more if the self-repair loop fires), not CPU. Run sequentially,
    a fleet of N vehicles costs N x that latency, which is what made batch
    mode feel slow. These calls are independent, so a small thread pool lets
    them wait concurrently - ``httpx`` releases the GIL while blocked on I/O,
    so threads (not processes) are the right tool and there is nothing to
    pickle.

    The pool is deliberately bounded: unbounded fan-out would just trade our
    latency for provider-side rate limiting. ``ThreadPoolExecutor.map``
    preserves input order, so the fleet rollup stays deterministic and each
    result still lines up with the spec it came from.
    """
    if not specs:
        return []
    if len(specs) == 1:
        # Don't pay for a thread pool to run a single spec.
        return [generate(specs[0])]

    pool = _get_executor()
    return list(pool.map(generate, specs))


def run_batch(specs: list[VehicleSpec]) -> BatchResponse:
    """Run the full pipeline for every spec and compute a fleet-level rollup."""
    results = _generate_all(specs)

    if not results:
        return BatchResponse(
            results=[],
            aggregate=BatchAggregate(
                vehicles=0,
                valid_count=0,
                validity_rate=0.0,
                avg_cycle_time_seconds=0.0,
                avg_critical_path_seconds=0.0,
                avg_speedup_factor=0.0,
            ),
        )

    valid_count = sum(1 for r in results if r.is_valid)

    # "Bottleneck ECU" per vehicle: whichever ECU's steps sum to the largest
    # share of that vehicle's estimated cycle time. Counting how often each
    # ECU wins that title across the fleet surfaces recurring hotspots.
    bottleneck_counter: Counter[str] = Counter()
    for result in results:
        per_ecu_time: Counter[str] = Counter()
        for step in result.program.steps:
            per_ecu_time[step.ecu_id] += step.estimated_seconds
        if per_ecu_time:
            bottleneck_counter[per_ecu_time.most_common(1)[0][0]] += 1

    # Most frequently recurring validation message across the whole fleet.
    issue_counter: Counter[str] = Counter()
    for result in results:
        for issue in result.validation:
            issue_counter[issue.message] += 1
    most_common_issue = issue_counter.most_common(1)[0][0] if issue_counter else None

    n = len(results)
    aggregate = BatchAggregate(
        vehicles=n,
        valid_count=valid_count,
        validity_rate=round(valid_count / n, 3),
        avg_cycle_time_seconds=round(
            sum(r.analytics.estimated_cycle_time_seconds for r in results) / n, 1
        ),
        avg_critical_path_seconds=round(
            sum(r.optimization.critical_path_seconds for r in results) / n, 1
        ),
        avg_speedup_factor=round(
            sum(r.optimization.speedup_factor for r in results) / n, 2
        ),
        bottleneck_ecus=[ecu for ecu, _ in bottleneck_counter.most_common(5)],
        most_common_issue=most_common_issue,
    )

    return BatchResponse(results=results, aggregate=aggregate)
