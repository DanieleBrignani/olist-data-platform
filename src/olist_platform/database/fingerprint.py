"""Content fingerprints of published tables: row count + md5 over every row in a canonical
order. Two runs produced "the same result" only if every table's fingerprint is identical -
much stronger than comparing row counts. Used by the idempotency tests and the benchmark.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, text

PUBLISHED_SCHEMAS = ("warehouse", "marts")


@dataclass(frozen=True)
class TableFingerprint:
    table: str
    rows: int
    md5: str


def tables_in(conn: Connection, schemas: tuple[str, ...]) -> list[str]:
    return list(
        conn.execute(
            text(
                "SELECT table_schema || '.' || table_name FROM information_schema.tables "
                "WHERE table_schema = ANY(:s) AND table_type = 'BASE TABLE' ORDER BY 1"
            ),
            {"s": list(schemas)},
        ).scalars()
    )


def fingerprint(
    conn: Connection, schemas: tuple[str, ...] = PUBLISHED_SCHEMAS
) -> dict[str, TableFingerprint]:
    """Canonical order = the row's own text representation, so the result does not depend on
    physical storage order, parallel plans or insertion order."""
    result = {}
    for table in tables_in(conn, schemas):
        rows, digest = conn.execute(
            text(
                f"SELECT count(*), coalesce(md5(string_agg(t::text, '|' ORDER BY t::text)), '') "
                f"FROM {table} AS t"
            )
        ).one()
        result[table] = TableFingerprint(table, rows, digest)
    return result


def diff(before: dict[str, TableFingerprint], after: dict[str, TableFingerprint]) -> list[str]:
    """Human-readable differences (empty list = identical)."""
    problems = [f"{t}: missing after" for t in before.keys() - after.keys()]
    problems += [f"{t}: new after" for t in after.keys() - before.keys()]
    for t in sorted(before.keys() & after.keys()):
        b, a = before[t], after[t]
        if b != a:
            problems.append(f"{t}: rows {b.rows}->{a.rows}, md5 {b.md5[:8]}->{a.md5[:8]}")
    return problems
