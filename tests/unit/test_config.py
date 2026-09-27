from __future__ import annotations

import pytest
from pydantic import ValidationError

from olist_platform.config import Role, Settings

REQUIRED = {
    "OLIST_ADMIN_PASSWORD": "a-secret",
    "OLIST_PIPELINE_PASSWORD": "p-secret",
    "OLIST_REPORTING_PASSWORD": "r-secret",
    "OLIST_MONITOR_PASSWORD": "m-secret",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("POSTGRES_HOST", "db.internal")
    monkeypatch.setenv("POSTGRES_PORT", "6543")
    monkeypatch.delenv("OLIST_DB_NAME", raising=False)  # integration conftest sets it


def test_each_role_gets_its_own_credentials(env: None) -> None:
    settings = Settings(_env_file=None)
    admin = settings.database_url(Role.ADMIN)
    reporting = settings.database_url(Role.REPORTING)

    assert (admin.username, admin.password) == ("olist_admin", "a-secret")
    assert (reporting.username, reporting.password) == ("olist_reporting", "r-secret")
    assert admin.host == "db.internal" and admin.port == 6543
    assert admin.database == "olist_dw"


def test_passwords_never_rendered_in_repr_or_url_string(env: None) -> None:
    settings = Settings(_env_file=None)
    assert "p-secret" not in repr(settings)
    assert "p-secret" not in str(settings.database_url(Role.PIPELINE))


def test_missing_credentials_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in REQUIRED:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
