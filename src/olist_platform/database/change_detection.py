"""Decide whether a rebuild can change the published result (docs/incremental.md).

A publication is a pure function of two inputs:
  1. the source snapshots: the sha256 of the latest loaded file of every source table;
  2. the logic: what transforms the data AND what decides whether it may be published
     (dbt project and tests, data contracts, staging engine, quality-gate policy).
If both are identical to those of the last successful publication and the published schemas
still exist, rebuilding would reproduce byte-identical tables judged by the same policy
(see the idempotency tests), so the flow skips staging, dbt, the gate and publication.
`--full-refresh` always rebuilds. A policy-only change forces a fresh gate decision; what
happens when the new policy fails is described in docs/incremental.md.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Connection, text

ROOT = Path(__file__).resolve().parents[3]
PUBLISHED_SCHEMAS = ("warehouse", "marts")

# Everything whose content determines, for given source files, the published tables or the
# decision to publish them. What is deliberately excluded, and why: docs/incremental.md.
TRANSFORM_INPUTS: tuple[str, ...] = (
    # transformation
    "dbt/dbt_project.yml",  # also the default severity of every dbt test
    "dbt/models/**/*",  # models and their test definitions (severity, tolerance)
    "dbt/macros/**/*",
    "dbt/tests/**/*",
    "data_contracts/*.yml",  # record rules, severities, reject limits
    "src/olist_platform/database/staging.py",  # rule engine: quarantine / flag / reject
    # validation policy: how contracts are read and how test results become a gate decision
    "src/olist_platform/validation/contracts.py",
    "src/olist_platform/transform/dbt_runner.py",
    "src/olist_platform/transform/warehouse.py",
    "src/olist_platform/quality/gate.py",
)


@dataclass(frozen=True)
class RebuildDecision:
    rebuild: bool
    reason: str
    fingerprint: str
    published_fingerprint: str | None


def transform_input_files(root: Path = ROOT) -> list[Path]:
    return sorted({p for pattern in TRANSFORM_INPUTS for p in root.glob(pattern) if p.is_file()})


def transform_fingerprint(root: Path = ROOT) -> str:
    """sha256 over path + content of every transformation and validation-policy input, in
    sorted order. Line endings are normalised so Windows and Linux checkouts agree."""
    digest = hashlib.sha256()
    for path in transform_input_files(root):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    return digest.hexdigest()


def source_snapshot(conn: Connection) -> dict[str, str]:
    """{source_table: sha256} of the file each table is currently built from."""
    return dict(
        conn.execute(
            text("SELECT source_table, sha256 FROM meta.current_source_files ORDER BY 1")
        ).all()
    )


def input_fingerprint(sources: dict[str, str], transform: str) -> str:
    payload = json.dumps({"sources": sources, "transform": transform}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def decide(
    current: str, published: str | None, published_schemas_exist: bool, full_refresh: bool
) -> RebuildDecision:
    """Pure decision rule (unit-tested exhaustively)."""
    if full_refresh:
        return RebuildDecision(True, "full_refresh_requested", current, published)
    if published is None:
        return RebuildDecision(True, "never_published", current, published)
    if not published_schemas_exist:
        return RebuildDecision(True, "published_schemas_missing", current, published)
    if current != published:
        return RebuildDecision(True, "inputs_changed", current, published)
    return RebuildDecision(False, "inputs_unchanged", current, published)


def current_fingerprint(conn: Connection, root: Path = ROOT) -> str:
    return input_fingerprint(source_snapshot(conn), transform_fingerprint(root))


def evaluate(conn: Connection, full_refresh: bool = False, root: Path = ROOT) -> RebuildDecision:
    current = current_fingerprint(conn, root)
    # Latest row of ANY action: each row stores the fingerprint of the version that is active
    # after it (a rollback row carries the fingerprint of the version it restored).
    published = conn.execute(
        text(
            "SELECT input_fingerprint FROM meta.publications "
            "ORDER BY published_at DESC, publication_id DESC LIMIT 1"
        )
    ).scalar()
    present = conn.execute(
        text("SELECT count(*) FROM pg_namespace WHERE nspname = ANY(:s)"),
        {"s": list(PUBLISHED_SCHEMAS)},
    ).scalar_one()
    return decide(current, published, present == len(PUBLISHED_SCHEMAS), full_refresh)
