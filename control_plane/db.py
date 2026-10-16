"""Database primitives for the control plane."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from alembic import command


class Base(DeclarativeBase):
    """Base for control-plane tables."""


def build_engine(database_url: str) -> Engine:
    """Create a database engine with SQLite's required thread setting."""

    arguments = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=arguments, pool_pre_ping=True)


def build_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


_MIGRATIONS_DIRECTORY = Path(__file__).resolve().parent / "migrations"
_LEGACY_APPLICATION_TABLES = {
    "collectors",
    "enrollments",
    "inventory_batches",
    "inventory_vms",
    "observation_batches",
    "observations",
    "reconnection_codes",
}
_BASELINE_REVISION = "0001_initial_schema"


def _alembic_config(connection) -> Config:
    """Configure Alembic with an already-open application database connection."""

    config = Config(str(_MIGRATIONS_DIRECTORY / "alembic.ini"))
    config.set_main_option("script_location", str(_MIGRATIONS_DIRECTORY))
    config.attributes["connection"] = connection
    return config


def run_migrations(engine: Engine) -> None:
    """Upgrade a blank or recognized legacy database to the tracked schema head.

    A legacy database created by earlier releases has the complete application
    schema but no Alembic revision marker. It is stamped at the immutable
    baseline before additive migrations run. Partial or unknown schemas fail
    closed instead of being guessed or overwritten.
    """

    with engine.begin() as connection:
        table_names = set(inspect(connection).get_table_names())
        config = _alembic_config(connection)
        if "alembic_version" not in table_names:
            existing_application_tables = table_names & _LEGACY_APPLICATION_TABLES
            if existing_application_tables:
                if existing_application_tables != _LEGACY_APPLICATION_TABLES:
                    raise RuntimeError(
                        "Database contains a partial legacy control-plane schema; "
                        "restore or migrate it with an approved procedure."
                    )
                command.stamp(config, _BASELINE_REVISION)
        command.upgrade(config, "head")
