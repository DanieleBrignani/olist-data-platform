"""Pipeline run registry: meta.pipeline_runs and meta.ingestion_events.

Every write here uses its own short transaction so that run metadata survives the
rollback of the data transaction it describes.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, Engine, text

from olist_platform.errors import PlatformError
from olist_platform.utils.logging import bind_context, get_logger

log = get_logger(__name__)


@dataclass
class RunTracker:
    """Handle yielded by `tracked_run`: set `.task` before each step so a failure is
    attributed to the step that raised."""

    pipeline_run_id: uuid.UUID
    task: str = "init"
    started: float = field(default_factory=time.perf_counter)

    def step(self, task: str) -> None:
        self.task = task
        bind_context(task=task)

    @property
    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self.started) * 1000, 1)


@contextmanager
def tracked_run(
    engine: Engine, flow_name: str, environment: str, dataset_version: int
) -> Iterator[RunTracker]:
    """Register a pipeline run; mark it success/failed (with failing task) on exit."""
    tracker = RunTracker(start_run(engine, flow_name, environment, dataset_version))
    bind_context(pipeline_run_id=str(tracker.pipeline_run_id), flow=flow_name)
    try:
        yield tracker
    except BaseException as exc:
        finish_run(engine, tracker.pipeline_run_id, failed_task=tracker.task, exc=exc)
        log.error(
            "pipeline_run_finished",
            status="failed",
            failed_task=tracker.task,
            error_type=getattr(exc, "error_type", type(exc).__name__),
            duration_ms=tracker.elapsed_ms,
        )
        raise
    finish_run(engine, tracker.pipeline_run_id)
    log.info("pipeline_run_finished", status="success", duration_ms=tracker.elapsed_ms)


# The flow's hard timeout is 2 h; a run still 'running' after this was killed (SIGKILL, host
# crash) and never reached its failure handler.
ABANDONED_AFTER = "3 hours"


def _close_abandoned_runs(conn: Connection) -> None:
    closed = conn.execute(
        text(
            "UPDATE meta.pipeline_runs SET status = 'failed', finished_at = now(), "
            "failed_task = coalesce(failed_task, 'unknown'), error_type = 'abandoned', "
            "error_message = 'still running after ' || :after || ': process presumably killed' "
            "WHERE status = 'running' AND started_at < now() - CAST(:after AS interval)"
        ),
        {"after": ABANDONED_AFTER},
    ).rowcount
    if closed:
        log.warning("abandoned_runs_closed", count=closed, after=ABANDONED_AFTER)


def start_run(
    engine: Engine,
    flow_name: str,
    environment: str,
    dataset_version: int,
    run_id: uuid.UUID | None = None,
) -> uuid.UUID:
    run_id = run_id or uuid.uuid4()
    with engine.begin() as conn:
        _close_abandoned_runs(conn)
        conn.execute(
            text(
                "INSERT INTO meta.pipeline_runs "
                "(pipeline_run_id, flow_name, environment, dataset_version, status) "
                "VALUES (:id, :flow, :env, :version, 'running')"
            ),
            {"id": run_id, "flow": flow_name, "env": environment, "version": dataset_version},
        )
    return run_id


def finish_run(
    engine: Engine,
    run_id: uuid.UUID,
    *,
    failed_task: str | None = None,
    exc: BaseException | None = None,
) -> None:
    status = "failed" if exc is not None else "success"
    error_type = None
    if exc is not None:
        error_type = exc.error_type if isinstance(exc, PlatformError) else type(exc).__name__
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE meta.pipeline_runs SET status = :status, finished_at = now(), "
                "failed_task = :task, error_type = :etype, error_message = :emsg "
                "WHERE pipeline_run_id = :id"
            ),
            {
                "id": run_id,
                "status": status,
                "task": failed_task,
                "etype": error_type,
                "emsg": str(exc)[:2000] if exc is not None else None,
            },
        )


def record_event(
    conn: Connection,
    run_id: uuid.UUID,
    *,
    task: str,
    event: str,
    status: str,
    source: str | None = None,
    source_file_id: int | None = None,
    rows_read: int | None = None,
    rows_written: int | None = None,
    rows_rejected: int | None = None,
    duration_ms: float | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        text(
            "INSERT INTO meta.ingestion_events (pipeline_run_id, source_file_id, task, source, "
            "event, status, rows_read, rows_written, rows_rejected, duration_ms, details) "
            "VALUES (:run, :sfid, :task, :source, :event, :status, :read, :written, :rejected, "
            ":ms, CAST(:details AS jsonb))"
        ),
        {
            "run": run_id,
            "sfid": source_file_id,
            "task": task,
            "source": source,
            "event": event,
            "status": status,
            "read": rows_read,
            "written": rows_written,
            "rejected": rows_rejected,
            "ms": duration_ms,
            "details": json.dumps(details or {}),
        },
    )
