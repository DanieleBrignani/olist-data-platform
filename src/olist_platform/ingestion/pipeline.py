"""Ingestion entrypoint: verify_manifest -> validate_source -> ingest_raw.

Each step is a plain function so the Prefect flow can wrap them as tasks with their own
retry policies; `run_ingestion` chains them for CLI use with the same run bookkeeping
(meta.pipeline_runs) the flow uses.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine

from olist_platform.database.locking import pipeline_lock
from olist_platform.database.staging import TableResult, load_staging
from olist_platform.errors import DataContractError
from olist_platform.ingestion.loader import FileLoadResult, ingest_file
from olist_platform.ingestion.manifest import Lock, load_lock, verify_against_lock
from olist_platform.ingestion.runs import tracked_run
from olist_platform.utils.logging import bind_context, get_logger
from olist_platform.validation.contracts import Contract
from olist_platform.validation.schema import check_file, enforce

log = get_logger(__name__)


@dataclass
class IngestionSummary:
    pipeline_run_id: uuid.UUID
    results: list[FileLoadResult]
    duration_ms: float

    @property
    def rows_loaded(self) -> int:
        return sum(r.rows_loaded for r in self.results)

    @property
    def rows_rejected(self) -> int:
        return sum(r.rows_rejected for r in self.results)


def verify_manifest(directory: Path, lock: Lock) -> dict[str, str]:
    bind_context(task="verify_manifest")
    return verify_against_lock(directory, lock)


def validate_source(directory: Path, contracts: dict[str, Contract]) -> None:
    """Check every file before writing anything, so one bad file cannot leave a mixed load."""
    bind_context(task="validate_source")
    failures = []
    for contract in contracts.values():
        try:
            enforce(check_file(directory / contract.file, contract))
        except DataContractError as exc:
            failures.append(str(exc))
    if failures:
        raise DataContractError("; ".join(failures))


def ingest_raw(
    engine: Engine,
    run_id: uuid.UUID,
    directory: Path,
    lock: Lock,
    checksums: dict[str, str],
    contracts: dict[str, Contract],
    force_reload: bool = False,
) -> list[FileLoadResult]:
    bind_context(task="ingest_raw")
    return [
        ingest_file(
            engine,
            run_id,
            contract,
            directory / contract.file,
            lock.entry(contract.file),
            checksums[contract.file],
            force_reload=force_reload,
        )
        for contract in contracts.values()
    ]


def run_ingestion(
    engine: Engine,
    directory: Path,
    contracts: dict[str, Contract],
    *,
    environment: str,
    force_reload: bool = False,
    lock: Lock | None = None,
) -> IngestionSummary:
    lock = lock or load_lock()
    with tracked_run(engine, "olist_ingestion", environment, lock.dataset_version) as run:
        run.step("acquire_pipeline_lock")
        with pipeline_lock("olist_ingestion"):
            run.step("verify_manifest")
            checksums = verify_manifest(directory, lock)
            run.step("validate_source")
            validate_source(directory, contracts)
            run.step("ingest_raw")
            results = ingest_raw(
                engine, run.pipeline_run_id, directory, lock, checksums, contracts, force_reload
            )
            summary = IngestionSummary(run.pipeline_run_id, results, run.elapsed_ms)
            log.info(
                "ingestion_summary",
                rows_written=summary.rows_loaded,
                rows_rejected=summary.rows_rejected,
                files_loaded=sum(r.status == "loaded" for r in results),
                files_skipped=sum(r.status == "skipped" for r in results),
                duration_ms=summary.duration_ms,
            )
    return summary


def run_staging(
    engine: Engine, contracts: dict[str, Contract], *, environment: str
) -> list[TableResult]:
    """load_staging as its own tracked run (the Prefect flow chains it after ingest_raw)."""
    version = next(iter(contracts.values())).dataset_version
    with tracked_run(engine, "olist_staging", environment, version) as run:
        run.step("acquire_pipeline_lock")
        with pipeline_lock("olist_staging"):
            run.step("load_staging")
            return load_staging(engine, run.pipeline_run_id, contracts)
