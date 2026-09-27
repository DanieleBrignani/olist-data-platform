"""Engine factory: one engine per role, never a shared superuser connection."""

from __future__ import annotations

from functools import cache

from sqlalchemy import Engine, create_engine

from olist_platform.config import Role, get_settings


@cache
def get_engine(role: Role) -> Engine:
    settings = get_settings()
    return create_engine(
        settings.database_url(role),
        pool_pre_ping=True,
        connect_args={"application_name": f"olist-platform:{role.value}", "connect_timeout": 10},
    )
