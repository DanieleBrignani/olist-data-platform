"""Integration tests run against the dedicated `olist_dw_test` database (created by
infra/postgres/init with the same roles and grants as olist_dw), never against real data."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import text

from olist_platform.cli import db_upgrade
from olist_platform.config import Role, get_settings
from olist_platform.database.engine import get_engine

TEST_DB = "olist_dw_test"
os.environ["OLIST_DB_NAME"] = TEST_DB


@pytest.fixture(scope="session", autouse=True)
def migrated_test_db() -> None:
    get_settings.cache_clear()
    assert get_settings().db_name == TEST_DB
    db_upgrade("head")
    get_engine.cache_clear()


@pytest.fixture
def clean_db() -> Iterator[None]:
    """Empty every meta/raw table (admin owns them) before a test that writes data."""
    with get_engine(Role.ADMIN).begin() as conn:
        tables = (
            conn.execute(
                text(
                    "SELECT schemaname || '.' || tablename FROM pg_tables "
                    "WHERE schemaname IN ('meta', 'raw', 'src')"
                )
            )
            .scalars()
            .all()
        )
        if tables:
            conn.execute(text(f"TRUNCATE {', '.join(tables)} CASCADE"))
    yield
