"""The `olist_refresh` Prefect flow: raw files -> published, quality-gated marts.

download_or_locate_source -> verify_manifest -> validate_source -> ingest_raw -> load_staging
-> dbt_build -> dbt_test -> quality_gate -> publish_marts -> publish_metrics

The whole flow is ONE pipeline run in meta.pipeline_runs (tracked_run): a failure is
recorded with the name of the task that failed. Tasks call the same functions the CLI uses,
so the orchestrated path and the tested path are identical code.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from prefect import flow, task
from sqlalchemy import text

from olist_platform.config import Role, get_settings
from olist_platform.database.engine import get_engine
from olist_platform.database.publish import publish as swap_published_schemas
from olist_platform.database.staging import load_staging as stage_src
from olist_platform.ingestion.manifest import LOCK_PATH, load_lock
from olist_platform.ingestion.pipeline import ingest_raw as ingest_files
from olist_platform.ingestion.pipeline import validate_source as check_sources
from olist_platform.ingestion.pipeline import verify_manifest as check_manifest
from olist_platform.ingestion.runs import tracked_run
from olist_platform.ingestion.source import locate_or_download
from olist_platform.transform.warehouse import dbt_build as build_models
from olist_platform.transform.warehouse import dbt_test as test_models
from olist_platform.transform.warehouse import quality_gate as gate
from olist_platform.utils.logging import configure_logging, get_logger
from olist_platform.validation.contracts import load_contracts
from orchestration.policies import no_retry, on_flow_failure, transient_retry

DEFAULT_DATA_ROOT = Path(
    os.environ.get("OLIST_DATA_ROOT", Path(__file__).resolve().parents[1] / "data")
)

log = get_logger("orchestration")


@task(name="download_or_locate_source", timeout_seconds=900, **transient_retry(3))
def download_or_locate_source(data_root: str) -> str:
    files = [c.file for c in load_contracts().values()]
    return str(locate_or_download(Path(data_root), files).directory)


@task(name="verify_manifest", timeout_seconds=600, **no_retry())
def verify_manifest(directory: str, lock_path: str) -> dict[str, str]:
    return check_manifest(Path(directory), load_lock(Path(lock_path)))


@task(name="validate_source", timeout_seconds=600, **no_retry())
def validate_source(directory: str) -> None:
    check_sources(Path(directory), load_contracts())


@task(name="ingest_raw", timeout_seconds=1800, **transient_retry(2))
def ingest_raw(
    run_id: uuid.UUID, directory: str, checksums: dict[str, str], force_reload: bool, lock_path: str
) -> dict[str, Any]:
    results = ingest_files(
        get_engine(Role.PIPELINE),
        run_id,
        Path(directory),
        load_lock(Path(lock_path)),
        checksums,
        load_contracts(),
        force_reload,
    )
    return {
        "loaded": sum(r.status == "loaded" for r in results),
        "skipped": sum(r.status == "skipped" for r in results),
        "rows_loaded": sum(r.rows_loaded for r in results),
        "rows_rejected": sum(r.rows_rejected for r in results),
    }


@task(name="load_staging", timeout_seconds=1800, **transient_retry(2))
def load_staging(run_id: uuid.UUID) -> dict[str, Any]:
    results = stage_src(get_engine(Role.PIPELINE), run_id, load_contracts())
    return {
        "src_rows": sum(r.src_rows for r in results),
        "quarantined": sum(r.quarantined for r in results),
        "flagged": sum(r.flagged for r in results),
    }


@task(name="dbt_build", timeout_seconds=1800, **transient_retry(1))
def dbt_build() -> dict[str, int]:
    return build_models().by_status()


@task(name="dbt_test", timeout_seconds=1800, **transient_retry(1))
def dbt_test() -> Any:
    return test_models()


@task(name="quality_gate", timeout_seconds=300, **no_retry())
def quality_gate(run_id: uuid.UUID, tests: Any) -> dict[str, int]:
    decision = gate(get_engine(Role.PIPELINE), run_id, tests)
    return {"tests": decision.evaluated, "warnings": len(decision.warnings)}


@task(name="publish_marts", timeout_seconds=300, **transient_retry(2))
def publish_marts(run_id: uuid.UUID) -> dict[str, int]:
    return swap_published_schemas(get_engine(Role.PIPELINE), run_id)


@task(name="publish_metrics", timeout_seconds=120, **transient_retry(2))
def publish_metrics(run_id: uuid.UUID) -> dict[str, Any]:
    """Summarise this run from the metadata tables (the system of record for metrics,
    ADR-0007) and emit it as one structured event; the exporter serves it to Prometheus."""
    with get_engine(Role.PIPELINE).connect() as conn:
        row = conn.execute(
            text("""
            SELECT
              (SELECT coalesce(sum(rows_read), 0) FROM meta.ingestion_events
                WHERE pipeline_run_id = :r AND task = 'ingest_raw') AS rows_read,
              (SELECT coalesce(sum(rows_written), 0) FROM meta.ingestion_events
                WHERE pipeline_run_id = :r AND task = 'load_staging') AS rows_written,
              (SELECT count(*) FROM meta.rejected_records
                WHERE pipeline_run_id = :r AND action = 'quarantined') AS rows_rejected,
              (SELECT count(*) FROM meta.dq_results
                WHERE pipeline_run_id = :r AND status IN ('warn', 'fail')) AS dq_failures,
              (SELECT max(published_at) FROM meta.publications
                WHERE pipeline_run_id = :r) AS published_at
        """),
            {"r": run_id},
        ).one()
    summary = dict(row._mapping)
    summary["published_at"] = (
        summary["published_at"].isoformat() if summary["published_at"] else None
    )
    log.info("pipeline_metrics", task="publish_metrics", **summary)
    return summary


@flow(
    name="olist_refresh",
    timeout_seconds=7200,
    log_prints=True,
    on_failure=[on_flow_failure],
    on_crashed=[on_flow_failure],
)
def olist_refresh(
    data_root: str = str(DEFAULT_DATA_ROOT),
    force_reload: bool = False,
    publish_enabled: bool = True,
    lock_path: str = str(LOCK_PATH),
) -> dict[str, Any]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.env, settings.log_file)
    version = load_lock(Path(lock_path)).dataset_version
    summary: dict[str, Any] = {}
    with tracked_run(get_engine(Role.PIPELINE), "olist_refresh", settings.env, version) as run:
        run_id = run.pipeline_run_id
        summary["pipeline_run_id"] = str(run_id)
        run.step("download_or_locate_source")
        directory = download_or_locate_source(data_root)
        run.step("verify_manifest")
        checksums = verify_manifest(directory, lock_path)
        run.step("validate_source")
        validate_source(directory)
        run.step("ingest_raw")
        summary["ingest"] = ingest_raw(run_id, directory, checksums, force_reload, lock_path)
        run.step("load_staging")
        summary["staging"] = load_staging(run_id)
        run.step("dbt_build")
        summary["dbt_build"] = dbt_build()
        run.step("dbt_test")
        tests = dbt_test()
        run.step("quality_gate")
        summary["quality_gate"] = quality_gate(run_id, tests)
        if publish_enabled:
            run.step("publish_marts")
            summary["published_tables"] = len(publish_marts(run_id))
        run.step("publish_metrics")
        summary["metrics"] = publish_metrics(run_id)
    return summary
