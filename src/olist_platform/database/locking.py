"""Database-level mutual exclusion for every operation that writes pipeline state.

The Prefect deployment's `limit=1` only serialises deployment runs. The CLI (`olist ingest`,
`stage`, `transform`, `warehouse`, `run`, `rollback-publish`, `db drop-derived`), a second
worker or a benchmark can reach the same database independently. Two of them interleaving can
publish a half-built warehouse: `dbt_build` empties `*_build` while another run is about to
swap it in. So every operation that writes raw, src, the build or published schemas, or the
publication metadata, holds ONE PostgreSQL session-level advisory lock for its whole duration.

* Identity: a fixed key per database (`LOCK_KEY`). Advisory locks are scoped to the database,
  so the platform and its isolated test database (olist_dw_test) never block each other.
* Connection: the lock lives on a dedicated, non-pooled connection (NullPool) in autocommit
  mode. It is never returned to a pool, and closing it ends the session, which makes
  PostgreSQL release the lock even if the process dies without unlocking.
* Waiting policy: bounded. A contender waits up to `wait_seconds` (default
  OLIST_PIPELINE_LOCK_WAIT_SECONDS, 60 s), then fails with PipelineBusyError, a transient
  error naming the holder's backend pid. `wait_seconds=0` rejects immediately.
* Nesting: re-entrant within a process. The flow holds the lock and its tasks (other threads
  of the same process) call `publish`, which enters again without a second session; the lock
  is released only when the outermost holder exits. One process runs one operation at a
  time (a CLI command, or one deployment run in its own subprocess).
* Readers are unaffected: advisory locks do not block SELECTs, so reporting and metrics keep
  working while a refresh runs.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from psycopg import errors as pg_errors
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from olist_platform.config import Role, get_settings
from olist_platform.errors import PipelineBusyError
from olist_platform.utils.logging import get_logger

log = get_logger(__name__)

LOCK_KEY = 0x6F6C697374  # "olist": one advisory-lock key per database
DEFAULT_WAIT_SECONDS = float(os.environ.get("OLIST_PIPELINE_LOCK_WAIT_SECONDS", "60"))


class _ProcessLock:
    """Re-entrancy bookkeeping for this process: one session holds the database lock."""

    def __init__(self) -> None:
        self.mutex = threading.Lock()
        self.depth = 0
        self.conn: Connection | None = None


_STATE = _ProcessLock()


def _holder(conn: Connection) -> str:
    """Who holds the lock (pid, application, since), for the contention message."""
    row = conn.execute(
        text(
            "SELECT a.pid, a.application_name, a.backend_start FROM pg_locks l "
            "JOIN pg_stat_activity a ON a.pid = l.pid "
            "WHERE l.locktype = 'advisory' AND l.granted "
            "AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
            "AND l.classid = :hi AND l.objid = :lo AND l.objsubid = 1"
        ),
        {"hi": LOCK_KEY >> 32, "lo": LOCK_KEY & 0xFFFFFFFF},
    ).first()
    if row is None:
        return "holder already gone"
    app = row.application_name or "unknown application"
    return f"pid {row.pid} ({app}, since {row.backend_start})"


def _acquire(operation: str, wait_seconds: float) -> Connection:
    engine = create_engine(
        get_settings().database_url(Role.PIPELINE),
        poolclass=NullPool,
        connect_args={"application_name": f"olist-lock:{operation}"[:63], "connect_timeout": 10},
    )
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        if wait_seconds <= 0:
            acquired = bool(
                conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar()
            )
        else:
            # lock_timeout bounds the wait of pg_advisory_lock like any heavyweight lock wait
            conn.execute(text(f"SET lock_timeout = '{int(wait_seconds * 1000)}ms'"))
            try:
                conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": LOCK_KEY})
                acquired = True
            except DBAPIError as exc:
                if not isinstance(exc.orig, pg_errors.LockNotAvailable):
                    raise
                acquired = False
            conn.execute(text("RESET lock_timeout"))
        if not acquired:
            holder = _holder(conn)
            log.warning("pipeline_lock_busy", operation=operation, holder=holder)
            raise PipelineBusyError(
                f"{operation}: another pipeline operation holds the database lock "
                f"({holder}); waited {wait_seconds:g} s"
            )
    except BaseException:
        conn.close()
        engine.dispose()
        raise
    log.info("pipeline_lock_acquired", operation=operation)
    return conn


def _release(conn: Connection) -> None:
    try:
        conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
    finally:
        # closing a NullPool connection ends the session: the lock is gone even if the
        # unlock above failed because the connection was already broken
        engine = conn.engine
        conn.close()
        engine.dispose()
        log.info("pipeline_lock_released")


@contextmanager
def pipeline_lock(operation: str, wait_seconds: float | None = None) -> Iterator[None]:
    """Hold the database-wide pipeline lock for the duration of the block (re-entrant)."""
    with _STATE.mutex:
        if _STATE.depth == 0:
            _STATE.conn = _acquire(
                operation, DEFAULT_WAIT_SECONDS if wait_seconds is None else wait_seconds
            )
        _STATE.depth += 1
    try:
        yield
    finally:
        with _STATE.mutex:
            _STATE.depth -= 1
            if _STATE.depth == 0 and _STATE.conn is not None:
                conn, _STATE.conn = _STATE.conn, None
                _release(conn)


def held_by_this_process() -> bool:
    return _STATE.depth > 0
