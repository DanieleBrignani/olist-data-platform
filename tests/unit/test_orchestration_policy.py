from __future__ import annotations

import psycopg
import pytest
from prefect.cache_policies import NO_CACHE
from sqlalchemy.exc import OperationalError

from olist_platform.errors import (
    DataContractError,
    ManifestMismatchError,
    QualityGateError,
    TransientError,
)
from olist_platform.transform.dbt_runner import DbtError
from orchestration import olist_flow
from orchestration.policies import is_retryable

TASKS = {
    "download_or_locate_source": olist_flow.download_or_locate_source,
    "verify_manifest": olist_flow.verify_manifest,
    "validate_source": olist_flow.validate_source,
    "ingest_raw": olist_flow.ingest_raw,
    "load_staging": olist_flow.load_staging,
    "dbt_build": olist_flow.dbt_build,
    "dbt_test": olist_flow.dbt_test,
    "quality_gate": olist_flow.quality_gate,
    "publish_marts": olist_flow.publish_marts,
    "publish_metrics": olist_flow.publish_metrics,
}
DETERMINISTIC = {"verify_manifest", "validate_source", "quality_gate"}


@pytest.mark.parametrize(
    "exc",
    [
        TransientError("network"),
        OperationalError("select 1", {}, Exception("connection refused")),
        psycopg.OperationalError("server closed the connection"),
        ConnectionResetError("reset"),
        TimeoutError("read timed out"),
    ],
)
def test_transient_failures_are_retryable(exc: BaseException) -> None:
    assert is_retryable(exc)


@pytest.mark.parametrize(
    "exc",
    [
        ManifestMismatchError("sha256"),
        DataContractError("column removed"),
        QualityGateError("critical"),
        DbtError("model failed"),
        ValueError("bug"),
        None,
    ],
)
def test_deterministic_failures_are_never_retried(exc: BaseException | None) -> None:
    assert not is_retryable(exc)


@pytest.mark.parametrize("name", list(TASKS))
def test_every_task_has_timeout_callback_and_no_cache(name: str) -> None:
    task = TASKS[name]
    assert task.name == name
    assert task.timeout_seconds and task.timeout_seconds > 0
    assert task.on_failure_hooks, "failure callback missing"
    assert task.cache_policy is NO_CACHE


@pytest.mark.parametrize("name", sorted(DETERMINISTIC))
def test_deterministic_steps_have_no_retries(name: str) -> None:
    assert TASKS[name].retries == 0


@pytest.mark.parametrize("name", sorted(set(TASKS) - DETERMINISTIC))
def test_retrying_steps_are_bounded_backoff_and_conditional(name: str) -> None:
    task = TASKS[name]
    assert 1 <= task.retries <= 3  # bounded
    delays = task.retry_delay_seconds
    assert isinstance(delays, list) and len(delays) == task.retries
    assert delays == sorted(delays) and (len(delays) == 1 or delays[-1] > delays[0])  # exponential
    assert task.retry_jitter_factor == 0.5
    assert task.retry_condition_fn is not None  # retries only on transient errors


def test_flow_has_timeout_and_failure_callbacks() -> None:
    flow = olist_flow.olist_refresh
    assert flow.timeout_seconds == 7200
    assert flow.on_failure_hooks and flow.on_crashed_hooks
