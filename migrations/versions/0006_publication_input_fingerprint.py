"""Record which inputs produced each publication.

meta.publications.input_fingerprint = sha256 over (current source-file checksums + the
transformation logic: dbt project, contracts, staging engine). The flow compares it with the
fingerprint of the current inputs to skip a rebuild that could not change the result
(docs/incremental.md).

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE meta.publications ADD COLUMN input_fingerprint char(64)")
    op.execute(
        "ALTER TABLE meta.publications ADD CONSTRAINT ck_publications_fingerprint "
        "CHECK (input_fingerprint ~ '^[0-9a-f]{64}$')"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE meta.publications DROP CONSTRAINT ck_publications_fingerprint")
    op.execute("ALTER TABLE meta.publications DROP COLUMN input_fingerprint")
