"""Create platform-owned schemas and least-privilege grants.

meta / raw / src are owned by olist_admin (DDL only through migrations).
olist_pipeline gets DML; olist_monitor gets read-only access to meta.
dbt-owned schemas (stg, int, *_build, warehouse, marts) are created by olist_pipeline at
runtime and are not managed here (docs/architecture.md section 3).

Revision ID: 0001
Revises:
Create Date: 2026-09-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PLATFORM_SCHEMAS = ("meta", "raw", "src")


def upgrade() -> None:
    for schema in PLATFORM_SCHEMAS:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema} AUTHORIZATION olist_admin")
        op.execute(f"REVOKE ALL ON SCHEMA {schema} FROM PUBLIC")
        op.execute(f"GRANT USAGE ON SCHEMA {schema} TO olist_pipeline")
        # Applies to every table/sequence olist_admin creates in later migrations
        op.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE olist_admin IN SCHEMA {schema} "
            "GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLES TO olist_pipeline"
        )
        op.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE olist_admin IN SCHEMA {schema} "
            "GRANT USAGE, SELECT ON SEQUENCES TO olist_pipeline"
        )

    op.execute("GRANT USAGE ON SCHEMA meta TO olist_monitor")
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE olist_admin IN SCHEMA meta "
        "GRANT SELECT ON TABLES TO olist_monitor"
    )


def downgrade() -> None:
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE olist_admin IN SCHEMA meta "
        "REVOKE SELECT ON TABLES FROM olist_monitor"
    )
    for schema in reversed(PLATFORM_SCHEMAS):
        op.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE olist_admin IN SCHEMA {schema} "
            "REVOKE ALL ON TABLES FROM olist_pipeline"
        )
        op.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE olist_admin IN SCHEMA {schema} "
            "REVOKE ALL ON SEQUENCES FROM olist_pipeline"
        )
        op.execute(f"DROP SCHEMA IF EXISTS {schema} RESTRICT")
