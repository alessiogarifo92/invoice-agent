"""Database engine and session factory, created lazily on first use.

Nothing here connects at import time, so /healthz and app startup never touch the
database (Neon scales to zero; a health check must not wake it up).
"""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Connection, Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def normalize_url(url: str) -> str:
    # Neon hands out postgresql://...; SQLAlchemy needs the psycopg (v3) driver name.
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


@lru_cache
def get_engine() -> Engine:
    return create_engine(normalize_url(get_settings().database_url), pool_pre_ping=True)


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine())


def get_connection() -> Iterator[Connection]:
    """FastAPI dependency: one connection per request, rolled back unless committed."""
    with get_engine().connect() as conn:
        yield conn
