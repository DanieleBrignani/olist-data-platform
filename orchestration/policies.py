"""Retry, timeout and failure-callback policy for every orchestrated task (ADR-0003).

Retries are decided by exception TYPE, never by a blanket count:
* TransientError, DB connection drops  -> bounded retries, exponential backoff + jitter
* anything else (DataContractError, ManifestMismatchError, QualityGateError, DbtError,
  programming errors)                   -> fail immediately; retrying cannot change the outcome
"""

from __future__ import annotations

from typing import Any

import psycopg
from prefect.cache_policies import NO_CACHE
from prefect.tasks import exponential_backoff
from sqlalchemy.exc import OperationalError as SAOperationalError

from olist_platform.errors import PlatformError, TransientError, is_transient
from olist_platform.utils.logging import get_logger

log = get_logger("orchestration")

TRANSIENT_TYPES: tuple[type[BaseException], ...] = (
    TransientError,
    SAOperationalError,  # connection refused / reset while the DB restarts
    psycopg.OperationalError,
    ConnectionError,
    TimeoutError,
)


def is_retryable(exc: BaseException | None) -> bool:
    return exc is not None and (is_transient(exc) or isinstance(exc, TRANSIENT_TYPES))


def _exception_of(state: Any) -> BaseException | None:
    try:
        state.result(raise_on_failure=True)
    except BaseException as exc:
        return exc
    return None


def retry_on_transient(task: Any, task_run: Any, state: Any) -> bool:
    """Prefect `retry_condition_fn`: retry only when the failure is transient."""
    exc = _exception_of(state)
    retry = is_retryable(exc)
    log.info(
        "retry_decision",
        task=getattr(task, "name", "?"),
        retry=retry,
        error_type=type(exc).__name__ if exc else None,
    )
    return retry


def on_task_failure(task: Any, task_run: Any, state: Any) -> None:
    """Failure callback: one structured, alertable event per terminally failed task."""
    exc = _exception_of(state)
    log.error(
        "task_failed",
        task=getattr(task, "name", "?"),
        error_type=exc.error_type
        if isinstance(exc, PlatformError)
        else type(exc).__name__
        if exc
        else None,
        retried=task_run.run_count > 1 if task_run else None,
        attempts=getattr(task_run, "run_count", None),
        retryable=is_retryable(exc),
        status="failed",
    )


def on_flow_failure(flow: Any, flow_run: Any, state: Any) -> None:
    """Flow-level failure/crash callback (the exported run metrics also count these runs)."""
    log.error(
        "flow_failed",
        flow=getattr(flow, "name", "?"),
        flow_run_id=str(getattr(flow_run, "id", "")),
        state=state.type.value,
        message=(state.message or "")[:500],
        status="failed",
    )


def transient_retry(retries: int, backoff_factor: float = 10.0) -> dict[str, Any]:
    """Task kwargs: `retries` attempts with exponential backoff (factor, 2x factor, 4x...)
    and 50% jitter, applied only to transient failures."""
    return {
        "retries": retries,
        "retry_delay_seconds": exponential_backoff(backoff_factor=backoff_factor),
        "retry_jitter_factor": 0.5,
        "retry_condition_fn": retry_on_transient,
        "on_failure": [on_task_failure],
        # pipeline steps have side effects: never skip one because its inputs look cached
        "cache_policy": NO_CACHE,
    }


def no_retry() -> dict[str, Any]:
    """Deterministic steps: a second attempt would fail identically."""
    return {"retries": 0, "on_failure": [on_task_failure], "cache_policy": NO_CACHE}
