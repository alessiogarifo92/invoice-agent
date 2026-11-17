"""Alembic environment. Migrations run with the OWNER connection, never the app role."""

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import get_settings

config = context.config


def _url() -> str:
    settings = get_settings()
    url = settings.owner_database_url or settings.database_url
    # Neon hands out postgresql://...; SQLAlchemy needs the psycopg (v3) driver name.
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
