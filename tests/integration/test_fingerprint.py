"""The fingerprint the idempotency tests rely on must be order-insensitive but
content-sensitive - otherwise "identical fingerprints" would prove nothing."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, text

from olist_platform.config import Role
from olist_platform.database.engine import get_engine
from olist_platform.database.fingerprint import TableFingerprint, diff, fingerprint

pytestmark = pytest.mark.integration
SCHEMA = "fp_test"


@pytest.fixture
def conn() -> Iterator[Connection]:
    with get_engine(Role.PIPELINE).connect() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {SCHEMA}"))
        c.execute(text(f"CREATE TABLE {SCHEMA}.a (id int, v text, amount numeric(12,2))"))
        yield c
        c.rollback()
        c.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        c.commit()


def load(conn: Connection, rows: list[tuple]) -> dict[str, TableFingerprint]:
    conn.execute(text(f"TRUNCATE {SCHEMA}.a"))
    for r in rows:
        conn.execute(
            text(f"INSERT INTO {SCHEMA}.a VALUES (:i, :v, :m)"), {"i": r[0], "v": r[1], "m": r[2]}
        )
    return fingerprint(conn, (SCHEMA,))


ROWS = [(1, "x", "10.00"), (2, "y", "20.50"), (3, None, "0.00")]


def test_same_content_in_any_order_gives_the_same_fingerprint(conn: Connection) -> None:
    assert load(conn, ROWS) == load(conn, list(reversed(ROWS)))


@pytest.mark.parametrize(
    "changed",
    [
        [(1, "x", "10.00"), (2, "y", "20.51"), (3, None, "0.00")],  # one cent
        [(1, "x", "10.00"), (2, "y", "20.50"), (3, "", "0.00")],  # NULL -> ''
        [(1, "x", "10.00"), (2, "y", "20.50")],  # one row lost
        [*ROWS, (3, None, "0.00")],  # one duplicate
    ],
)
def test_any_content_change_changes_the_fingerprint(conn: Connection, changed: list) -> None:
    before, after = load(conn, ROWS), load(conn, changed)
    assert diff(before, after), "fingerprint failed to detect a change"


def test_diff_reports_missing_and_new_tables() -> None:
    a = {"s.t1": TableFingerprint("s.t1", 1, "aa")}
    b = {"s.t2": TableFingerprint("s.t2", 1, "aa")}
    assert diff(a, b) == ["s.t1: missing after", "s.t2: new after"]
    assert diff(a, a) == []
