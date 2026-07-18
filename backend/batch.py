"""Batch mode: run several vehicle specs through the pipeline and aggregate.

A single spec's optimisation numbers are interesting; a fleet's are what a
process engineer actually acts on. This module runs ``run_pipeline`` once per
spec and rolls the results up into fleet-level findings: overall validity
rate, average cycle time and speedup, which ECUs most often dominate a
vehicle's program (a proxy for where to focus commissioning-process
improvement work), and the most frequently recurring validation issue.
"""

from __future__ import annotations

from collections import Counter

from .generator import generate
from .models import BatchAggregate, BatchResponse, VehicleSpec


def run_batch(specs: list[VehicleSpec]) -> BatchResponse:
    """Run the full pipeline for every spec and compute a fleet-level rollup."""
    results = [generate(spec) for spec in specs]

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
