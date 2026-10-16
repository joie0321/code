"""Alembic environment for the control-plane database schema."""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from control_plane import models  # noqa: F401 - registers metadata for autogenerate tooling.
from control_plane.config import ControlPlaneSettings
from control_plane.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without a live connection for explicitly configured tooling."""

    settings = ControlPlaneSettings()
    settings.validate_runtime()
    context.configure(
        url=settings.database_url_for_engine(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations using the application connection when supplied."""

    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        context.configure(
            connection=supplied_connection, target_metadata=target_metadata, compare_type=True
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    settings = ControlPlaneSettings()
    settings.validate_runtime()
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = settings.database_url_for_engine()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
