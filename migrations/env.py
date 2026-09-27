"""Alembic environment. Always runs as the olist_admin role (docs/adr/0009)."""

from __future__ import annotations

import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from olist_platform.config import Role, get_settings

# Plain `alembic ...` CLI: use alembic.ini's logging. Under `olist db upgrade` the JSON logging
# is already configured, so it is left untouched.
if context.config.config_file_name and not logging.getLogger().handlers:
    fileConfig(context.config.config_file_name, disable_existing_loggers=False)


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url(Role.ADMIN).render_as_string(hide_password=True),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(get_settings().database_url(Role.ADMIN), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
