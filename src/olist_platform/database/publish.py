"""publish_marts: atomic build-then-swap of warehouse and marts (ADR-0005).

In ONE transaction:
  1. refuse unless meta.quality_gate_decisions says PASS for this pipeline run
     (defence in depth: the orchestrator already stops on a FAIL);
  2. drop the previous *_prev schemas, demote the published schemas to *_prev
     (and revoke reporting access from them), promote *_build to the published names;
  3. grant the reporting role read access to the new published schemas;
  4. record the publication (row counts + max business event time) in meta.publications.

Readers see either the old or the new warehouse, never a mix. `rollback` swaps *_prev back.
"""

from __future__ import annotations

import json
import uuid

from psycopg import errors as pg_errors
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from olist_platform.database.change_detection import current_fingerprint
from olist_platform.database.locking import pipeline_lock
from olist_platform.errors import DeterministicError, QualityGateError, TransientError
from olist_platform.utils.logging import get_logger

# build schema -> published schema
SCHEMAS = {"warehouse_build": "warehouse", "marts_build": "marts"}
REPORTING_ROLE = "olist_reporting"
LOCK_TIMEOUT = "10s"

log = get_logger(__name__)


def _schema_exists(conn: Connection, name: str) -> bool:
    return bool(
        conn.execute(text("SELECT 1 FROM pg_namespace WHERE nspname = :n"), {"n": name}).scalar()
    )


def _row_counts(conn: Connection, schemas: list[str]) -> dict[str, int]:
    tables = (
        conn.execute(
            text(
                "SELECT table_schema || '.' || table_name FROM information_schema.tables "
                "WHERE table_schema = ANY(:s) AND table_type = 'BASE TABLE' ORDER BY 1"
            ),
            {"s": schemas},
        )
        .scalars()
        .all()
    )
    return {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in tables}


def _grant_reporting(conn: Connection, schema: str) -> None:
    conn.execute(text(f"GRANT USAGE ON SCHEMA {schema} TO {REPORTING_ROLE}"))
    conn.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {REPORTING_ROLE}"))


def _revoke_reporting(conn: Connection, schema: str) -> None:
    conn.execute(text(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {REPORTING_ROLE}"))
    conn.execute(text(f"REVOKE ALL ON SCHEMA {schema} FROM {REPORTING_ROLE}"))


def _record(
    conn: Connection, run_id: uuid.UUID, action: str, fingerprint: str | None
) -> dict[str, int]:
    """`fingerprint` = inputs of the version that is ACTIVE after this action
    (change_detection compares the latest row with the current inputs)."""
    counts = _row_counts(conn, list(SCHEMAS.values()))
    max_event = conn.execute(text("SELECT max(purchased_at) FROM warehouse.fct_orders")).scalar()
    conn.execute(
        text(
            "INSERT INTO meta.publications (pipeline_run_id, action, table_row_counts, "
            "source_max_event_at, input_fingerprint) "
            "VALUES (:run, :action, CAST(:counts AS jsonb), :max_event, :fp)"
        ),
        {
            "run": run_id,
            "action": action,
            "counts": json.dumps(counts),
            "max_event": max_event,
            "fp": fingerprint,
        },
    )
    return counts


def _locked(func):
    """Map lock timeouts (a long-running reader holds the schema) to TransientError."""

    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except DBAPIError as exc:
            if isinstance(exc.orig, pg_errors.LockNotAvailable):
                raise TransientError(f"publication lock timeout ({LOCK_TIMEOUT})") from exc
            raise

    return wrapper


@_locked
def publish(
    engine: Engine, run_id: uuid.UUID, input_fingerprint: str | None = None
) -> dict[str, int]:
    """`input_fingerprint`: the inputs this build was made from (the flow passes the value it
    evaluated before building). When omitted it is computed from the current inputs."""
    with pipeline_lock("publish"), engine.begin() as conn:
        conn.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
        decision = conn.execute(
            text("SELECT decision FROM meta.quality_gate_decisions WHERE pipeline_run_id = :run"),
            {"run": run_id},
        ).scalar()
        if decision != "PASS":
            raise QualityGateError(
                f"refusing to publish: quality gate decision for run {run_id} is {decision!r}"
            )
        missing = [b for b in SCHEMAS if not _schema_exists(conn, b)]
        if missing:
            raise DeterministicError(f"nothing to publish: build schemas missing {missing}")

        for build, published in SCHEMAS.items():
            conn.execute(text(f"DROP SCHEMA IF EXISTS {published}_prev CASCADE"))
            if _schema_exists(conn, published):
                _revoke_reporting(conn, published)
                conn.execute(text(f"ALTER SCHEMA {published} RENAME TO {published}_prev"))
            conn.execute(text(f"ALTER SCHEMA {build} RENAME TO {published}"))
            _grant_reporting(conn, published)
        if input_fingerprint is None:
            input_fingerprint = current_fingerprint(conn)
        counts = _record(conn, run_id, "publish", input_fingerprint)
    log.info(
        "marts_published",
        task="publish_marts",
        status="ok",
        tables=len(counts),
        rows_written=sum(counts.values()),
    )
    return counts


@_locked
def rollback(engine: Engine, run_id: uuid.UUID) -> dict[str, int]:
    """Swap the published schemas with *_prev (a second rollback re-applies the newer one)."""
    with pipeline_lock("rollback"), engine.begin() as conn:
        conn.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
        for published in SCHEMAS.values():
            if not (_schema_exists(conn, published) and _schema_exists(conn, f"{published}_prev")):
                raise DeterministicError(f"cannot roll back: {published}_prev does not exist")
        for published in SCHEMAS.values():
            _revoke_reporting(conn, published)
            conn.execute(text(f"ALTER SCHEMA {published} RENAME TO {published}_swap"))
            conn.execute(text(f"ALTER SCHEMA {published}_prev RENAME TO {published}"))
            conn.execute(text(f"ALTER SCHEMA {published}_swap RENAME TO {published}_prev"))
            _grant_reporting(conn, published)
        # the restored version is the one that was active before the latest action
        restored = conn.execute(
            text(
                "SELECT input_fingerprint FROM meta.publications "
                "ORDER BY published_at DESC, publication_id DESC OFFSET 1 LIMIT 1"
            )
        ).scalar()
        counts = _record(conn, run_id, "rollback", restored)
    log.info("marts_rolled_back", task="publish_marts", status="ok", tables=len(counts))
    return counts
