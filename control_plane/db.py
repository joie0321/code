"""Database primitives for the control plane."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    """Base for control-plane tables."""


def build_engine(database_url: str) -> Engine:
    """Create a database engine with SQLite's required thread setting."""

    arguments = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=arguments, pool_pre_ping=True)


def build_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)
