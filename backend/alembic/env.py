"""Alembic environment. Migrations run with the OWNER connection, never the app role."""

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import get_settings
from app.db import normalize_url

config = context.config


def _url() -> str:
    # Programmatic callers (the test suite) pass their own database in config.attributes.
    url: str | None = config.attributes.get("database_url")
    if url is None:
        settings = get_settings()
        url = settings.owner_database_url or settings.database_url
    return normalize_url(url)


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
