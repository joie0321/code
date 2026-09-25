"""Database primitives for the control plane."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    """Base for control-plane tables."""


def build_engine(database_url: str) -> Engine:
    """Create a database engine with SQLite's required thread setting."""

    arguments = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=arguments, pool_pre_ping=True)


def build_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


def upgrade_schema(engine: Engine) -> None:
    """Apply the small, additive PostgreSQL compatibility upgrade required by the API."""

    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as connection:
        current_width = connection.execute(
            text(
                "SELECT character_maximum_length "
                "FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'observations' "
                "AND column_name = 'collector_type'"
            )
        ).scalar_one_or_none()
        if current_width is not None and current_width < 32:
            connection.execute(
                text("ALTER TABLE observations ALTER COLUMN collector_type TYPE VARCHAR(32)")
            )
