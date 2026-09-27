"""E2E tests share the integration DB fixtures. Importing the integration conftest also points
the whole process at the isolated `olist_dw_test` database, so the real flow under test can
never write to the warehouse holding real data."""

from tests.integration.conftest import clean_db, migrated_test_db  # noqa: F401
