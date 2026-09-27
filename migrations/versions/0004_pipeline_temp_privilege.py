"""Grant TEMPORARY on the warehouse database to olist_pipeline only.

The bootstrap revokes every database privilege from PUBLIC (including TEMPORARY), which is
correct for reporting/monitoring roles. The staging engine evaluates rules in temp tables,
so the pipeline role needs TEMPORARY explicitly. Found when the first real `olist stage`
run failed with "permission denied to create temporary tables" (docs/what-failed.md).

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _grant(verb: str) -> None:
    preposition = "TO" if verb == "GRANT" else "FROM"
    op.execute(
        "DO $$ BEGIN EXECUTE format("
        f"'{verb} TEMPORARY ON DATABASE %I {preposition} olist_pipeline', current_database()"
        "); END $$"
    )


def upgrade() -> None:
    _grant("GRANT")


def downgrade() -> None:
    _grant("REVOKE")
