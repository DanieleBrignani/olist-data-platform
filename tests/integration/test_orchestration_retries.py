"""The retry policy, executed by a real Prefect engine (ephemeral test server).

Uses the SAME policy factories as the production tasks, with zero backoff so tests are fast.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from prefect import flow, task
from prefect.testing.utilities import prefect_test_harness

from olist_platform.errors import DataContractError, TransientError
from orchestration.policies import no_retry, transient_retry

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def prefect_backend() -> Iterator[None]:
    with prefect_test_harness():
        yield


def run_task_in_flow(fails_before_success: int, exc: type[Exception], policy: dict) -> tuple:
    calls = {"n": 0}

    @task(**policy)
    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] <= fails_before_success:
            raise exc("simulated")
        return "ok"

    @flow
    def wrapper() -> str:
        return flaky()

    try:
        return wrapper(), calls["n"]
    except Exception as error:
        return error, calls["n"]


def test_transient_error_is_retried_until_success() -> None:
    result, attempts = run_task_in_flow(2, TransientError, transient_retry(3, backoff_factor=0))
    assert (result, attempts) == ("ok", 3)


def test_transient_retries_are_bounded() -> None:
    result, attempts = run_task_in_flow(99, TransientError, transient_retry(2, backoff_factor=0))
    assert isinstance(result, TransientError)
    assert attempts == 3  # 1 try + 2 retries, then give up


def test_deterministic_error_is_not_retried_even_when_retries_are_configured() -> None:
    result, attempts = run_task_in_flow(1, DataContractError, transient_retry(3, backoff_factor=0))
    assert isinstance(result, DataContractError)
    assert attempts == 1


def test_no_retry_policy_runs_once() -> None:
    result, attempts = run_task_in_flow(1, TransientError, no_retry())
    assert isinstance(result, TransientError)
    assert attempts == 1
