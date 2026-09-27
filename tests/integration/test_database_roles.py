"""Least-privilege guarantees from docs/adr/0009, asserted against the running database."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from olist_platform.config import Role
from olist_platform.database.engine import get_engine

pytestmark = pytest.mark.integration


def _schema_priv(role: Role, schema: str, privilege: str) -> bool:
    with get_engine(Role.ADMIN).connect() as conn:
        return conn.execute(
            text("select has_schema_privilege(:role, :schema, :priv)"),
            {"role": role.value, "schema": schema, "priv": privilege},
        ).scalar_one()


@pytest.mark.parametrize("role", list(Role))
def test_every_role_can_connect_as_itself(role: Role) -> None:
    with get_engine(role).connect() as conn:
        assert conn.execute(text("select current_user")).scalar_one() == role.value


def test_platform_schemas_are_owned_by_admin() -> None:
    with get_engine(Role.ADMIN).connect() as conn:
        owners = dict(
            conn.execute(
                text(
                    "select nspname, pg_get_userbyid(nspowner) from pg_namespace "
                    "where nspname in ('meta', 'raw', 'src')"
                )
            ).all()
        )
    assert owners == {"meta": "olist_admin", "raw": "olist_admin", "src": "olist_admin"}


@pytest.mark.parametrize("schema", ["meta", "raw", "src"])
def test_pipeline_can_use_but_not_alter_platform_schemas(schema: str) -> None:
    assert _schema_priv(Role.PIPELINE, schema, "USAGE")
    assert not _schema_priv(Role.PIPELINE, schema, "CREATE")


@pytest.mark.parametrize("schema", ["meta", "raw", "src", "public"])
def test_reporting_cannot_reach_unpublished_schemas(schema: str) -> None:
    assert not _schema_priv(Role.REPORTING, schema, "USAGE")
    assert not _schema_priv(Role.REPORTING, schema, "CREATE")


def test_monitor_reads_meta_only() -> None:
    assert _schema_priv(Role.MONITOR, "meta", "USAGE")
    assert not _schema_priv(Role.MONITOR, "raw", "USAGE")
    assert not _schema_priv(Role.MONITOR, "src", "USAGE")


def test_only_pipeline_may_create_dbt_schemas() -> None:
    with get_engine(Role.ADMIN).connect() as conn:
        rows = dict(
            conn.execute(
                text(
                    "select r, has_database_privilege(r, current_database(), 'CREATE') "
                    "from unnest(array['olist_pipeline','olist_reporting','olist_monitor']) as r"
                )
            ).all()
        )
    assert rows == {"olist_pipeline": True, "olist_reporting": False, "olist_monitor": False}


@pytest.mark.parametrize(
    ("role", "expected"),
    [("olist_pipeline", True), ("olist_reporting", False), ("olist_monitor", False)],
)
def test_only_pipeline_may_create_temp_tables(role: str, expected: bool) -> None:
    with get_engine(Role.ADMIN).connect() as conn:
        allowed = conn.execute(
            text("select has_database_privilege(:r, current_database(), 'TEMPORARY')"),
            {"r": role},
        ).scalar_one()
    assert allowed is expected


def test_reporting_cannot_read_src_or_meta_tables() -> None:
    for schema in ("src", "meta"):
        assert not _schema_priv(Role.REPORTING, schema, "USAGE")
