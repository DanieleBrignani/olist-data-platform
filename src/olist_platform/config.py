"""Runtime configuration, read exclusively from environment variables (.env in local dev).

Each database role has its own credential so that every component connects with the least
privilege it needs (docs/adr/0009-least-privilege-roles.md).
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Role(StrEnum):
    ADMIN = "olist_admin"
    PIPELINE = "olist_pipeline"
    REPORTING = "olist_reporting"
    MONITOR = "olist_monitor"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    env: str = Field("local", alias="OLIST_ENV")

    postgres_host: str = Field("localhost", alias="POSTGRES_HOST")
    postgres_port: int = Field(5432, alias="POSTGRES_PORT")
    db_name: str = Field("olist_dw", alias="OLIST_DB_NAME")

    admin_password: SecretStr = Field(alias="OLIST_ADMIN_PASSWORD")
    pipeline_password: SecretStr = Field(alias="OLIST_PIPELINE_PASSWORD")
    reporting_password: SecretStr = Field(alias="OLIST_REPORTING_PASSWORD")
    monitor_password: SecretStr = Field(alias="OLIST_MONITOR_PASSWORD")

    log_level: str = Field("INFO", alias="OLIST_LOG_LEVEL")
    log_file: str | None = Field(None, alias="OLIST_LOG_FILE")

    def _password(self, role: Role) -> SecretStr:
        return {
            Role.ADMIN: self.admin_password,
            Role.PIPELINE: self.pipeline_password,
            Role.REPORTING: self.reporting_password,
            Role.MONITOR: self.monitor_password,
        }[role]

    def database_url(self, role: Role) -> URL:
        """SQLAlchemy URL for `role`; URL.create means passwords are never string-formatted."""
        return URL.create(
            drivername="postgresql+psycopg",
            username=role.value,
            password=self._password(role).get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.db_name,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # populated from the environment
