"""dbt_build -> dbt_test -> quality_gate -> publish_marts.

Each step is a plain function (the Prefect flow wraps them as tasks in Phase 8);
`run_warehouse` chains them under one tracked pipeline run for CLI use.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import Engine

from olist_platform.database.publish import publish
from olist_platform.ingestion.runs import tracked_run
from olist_platform.quality.gate import (
    GateDecision,
    enforce,
    evaluate,
    persist_results,
    results_from_dbt,
)
from olist_platform.transform.dbt_runner import DbtError, DbtRun, run_dbt


@dataclass
class WarehouseSummary:
    pipeline_run_id: uuid.UUID
    build: DbtRun
    tests: DbtRun
    decision: GateDecision
    published_rows: dict[str, int] | None


def dbt_build(target_path: str | None = None) -> DbtRun:
    """Build every model (views + tables into *_build schemas). Model errors are fatal."""
    return run_dbt(["run"], target_path=target_path)


def dbt_test(target_path: str | None = None) -> DbtRun:
    """Run every data test, storing failing rows. Test FAILURES are data, not errors:
    they are judged by the quality gate. Only a run that produced no results raises."""
    run = run_dbt(["test"], target_path=target_path, raise_on_failure=False)
    if not run.nodes:
        raise DbtError("dbt test produced no results (compilation or connection error)")
    return run


def quality_gate(engine: Engine, run_id: uuid.UUID, tests: DbtRun) -> GateDecision:
    results = results_from_dbt(tests)
    decision = evaluate(results)
    persist_results(engine, run_id, results, decision)  # evidence first, then enforce
    enforce(decision)
    return decision


def run_warehouse(
    engine: Engine,
    *,
    environment: str,
    dataset_version: int,
    publish_marts: bool = True,
    target_path: str | None = None,
) -> WarehouseSummary:
    with tracked_run(engine, "olist_warehouse", environment, dataset_version) as run:
        run.step("dbt_build")
        build = dbt_build(target_path)
        run.step("dbt_test")
        tests = dbt_test(target_path)
        run.step("quality_gate")
        decision = quality_gate(engine, run.pipeline_run_id, tests)
        published = None
        if publish_marts:
            run.step("publish_marts")
            published = publish(engine, run.pipeline_run_id)
        return WarehouseSummary(run.pipeline_run_id, build, tests, decision, published)
