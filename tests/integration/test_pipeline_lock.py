"""The database-wide pipeline lock (src/olist_platform/database/locking.py), exercised with
independent PostgreSQL sessions and separate OS processes, never with mocks."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from olist_platform.config import Role, get_settings
from olist_platform.database import locking
from olist_platform.database.engine import get_engine
from olist_platform.database.locking import LOCK_KEY, pipeline_lock
from olist_platform.database.publish import publish, rollback
from olist_platform.errors import PipelineBusyError, TransientError
from olist_platform.ingestion.pipeline import run_ingestion, run_staging
from olist_platform.ingestion.runs import start_run
from olist_platform.transform.warehouse import run_warehouse
from olist_platform.validation.contracts import load_contracts
from tests.lockholder import HOLDER, other_process_holding_the_lock, wait_for

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]


def lock_sessions() -> int:
    """Number of sessions currently holding the pipeline lock in this database."""
    with get_engine(Role.ADMIN).connect() as conn:
        return conn.execute(
            text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
                "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
                "AND classid = :hi AND objid = :lo AND objsubid = 1"
            ),
            {"hi": LOCK_KEY >> 32, "lo": LOCK_KEY & 0xFFFFFFFF},
        ).scalar_one()


def free_for_an_independent_session() -> bool:
    """Can a session that is not this process's lock connection take the lock right now?"""
    with get_engine(Role.ADMIN).connect() as conn:
        got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar()
        if got:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
        return bool(got)


def test_two_processes_never_hold_the_lock_together() -> None:
    with other_process_holding_the_lock() as holder:
        with (
            pytest.raises(PipelineBusyError, match=r"pid \d+") as busy,
            pipeline_lock("contender", wait_seconds=0),
        ):
            pytest.fail("acquired a lock held by another process")
        assert isinstance(busy.value, TransientError)  # retrying later can succeed
        assert busy.value.error_type == "pipeline_busy"
        assert lock_sessions() == 1
        holder.stdin.close()
        wait_for(holder, "released")
        holder.wait(timeout=30)
    with pipeline_lock("contender", wait_seconds=0):
        assert lock_sessions() == 1
    assert lock_sessions() == 0


def test_bounded_wait_gives_up_after_the_timeout() -> None:
    with other_process_holding_the_lock():
        started = time.monotonic()
        with (
            pytest.raises(PipelineBusyError, match="waited 1 s"),
            pipeline_lock("contender", wait_seconds=1),
        ):
            pass
        assert 0.9 <= time.monotonic() - started < 10


def test_a_waiting_contender_proceeds_when_the_holder_finishes() -> None:
    script = HOLDER.replace("sys.stdin.readline()", "import time; time.sleep(2)")
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, env=os.environ.copy()
    )
    wait_for(proc, "held")
    started = time.monotonic()
    with pipeline_lock("contender", wait_seconds=30):
        waited = time.monotonic() - started
    proc.wait(timeout=30)
    assert 0.5 < waited < 30


def test_the_lock_is_released_when_the_owning_process_is_killed() -> None:
    with other_process_holding_the_lock() as holder:
        holder.kill()  # SIGKILL: no finally, no unlock; only the session's end frees it
        holder.wait(timeout=30)
        with pipeline_lock("after_kill", wait_seconds=10):
            assert lock_sessions() == 1


def test_the_lock_is_released_on_exceptions_and_never_left_in_a_pool() -> None:
    with pytest.raises(RuntimeError, match="boom"), pipeline_lock("failing", wait_seconds=0):
        assert lock_sessions() == 1
        raise RuntimeError("boom")
    assert lock_sessions() == 0
    assert not locking.held_by_this_process()
    assert free_for_an_independent_session()


def test_nested_acquisition_neither_deadlocks_nor_releases_early() -> None:
    with pipeline_lock("outer", wait_seconds=0):
        with pipeline_lock("inner", wait_seconds=0):
            assert lock_sessions() == 1  # one session, not two
        assert lock_sessions() == 1  # the inner exit did not release it
        assert not free_for_an_independent_session()
    assert lock_sessions() == 0
    assert free_for_an_independent_session()


def test_readers_are_not_blocked_while_the_lock_is_held() -> None:
    with other_process_holding_the_lock():
        for role in (Role.REPORTING, Role.MONITOR, Role.PIPELINE):
            with get_engine(role).connect() as conn:
                conn.execute(text("SET statement_timeout = '5s'"))
                assert conn.execute(text("SELECT count(*) FROM pg_namespace")).scalar_one() > 0
        with get_engine(Role.MONITOR).connect() as conn:  # the metrics exporter's role
            conn.execute(text("SET statement_timeout = '5s'"))
            conn.execute(text("SELECT count(*) FROM meta.pipeline_runs")).scalar_one()


def _entry_points(tmp_path: Path) -> dict[str, object]:
    engine = get_engine(Role.PIPELINE)
    contracts = load_contracts()
    env = get_settings().env
    return {
        "olist_ingestion": lambda: run_ingestion(engine, tmp_path, contracts, environment=env),
        "olist_staging": lambda: run_staging(engine, contracts, environment=env),
        "olist_warehouse": lambda: run_warehouse(engine, environment=env, dataset_version=2),
        "publish": lambda: publish(engine, uuid.uuid4()),
        "rollback": lambda: rollback(engine, uuid.uuid4()),
    }


@pytest.mark.parametrize(
    "operation", ["olist_ingestion", "olist_staging", "olist_warehouse", "publish", "rollback"]
)
def test_every_writing_entry_point_waits_for_the_lock_and_writes_nothing(
    operation: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(locking, "DEFAULT_WAIT_SECONDS", 0.5)
    with get_engine(Role.ADMIN).connect() as conn:
        schemas_before = set(conn.execute(text("SELECT nspname FROM pg_namespace")).scalars())
    with other_process_holding_the_lock(), pytest.raises(PipelineBusyError):
        _entry_points(tmp_path)[operation]()
    with get_engine(Role.ADMIN).connect() as conn:
        assert set(conn.execute(text("SELECT nspname FROM pg_namespace")).scalars()) == (
            schemas_before
        )
        assert conn.execute(text("SELECT count(*) FROM meta.publications")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM meta.source_files")).scalar_one() == 0
        tracked = conn.execute(
            text("SELECT failed_task, error_type FROM meta.pipeline_runs WHERE status = 'failed'")
        ).all()
    if operation.startswith("olist_"):  # the CLI-level runs record why they stopped
        assert tracked == [("acquire_pipeline_lock", "pipeline_busy")]


def test_a_failed_holder_does_not_block_the_next_operation() -> None:
    engine = get_engine(Role.PIPELINE)
    run_id = start_run(engine, "probe", "test", 2)
    with (
        pytest.raises(PipelineBusyError),
        other_process_holding_the_lock(),
        pipeline_lock("contender", wait_seconds=0),
    ):
        pass
    with pytest.raises(ZeroDivisionError), pipeline_lock("crashing_holder", wait_seconds=0):
        _ = 1 / 0
    with pipeline_lock("next_operation", wait_seconds=0):
        assert run_id is not None
