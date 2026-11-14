"""Shared fixtures.

DB-backed tests never touch a shared database. From the admin URL in
TEST_ADMIN_DATABASE_URL they create a migrated template database once per session
(with a uniquely named app role, since roles are cluster-wide), then clone a fresh
database from it for every test, and drop everything at the end.

Without TEST_ADMIN_DATABASE_URL the DB tests are skipped, unless REQUIRE_DB_TESTS=1
(CI), which turns the skip into a failure.
"""

import os
import secrets
from collections.abc import Iterator
from dataclasses import dataclass

import psycopg
import pytest
from alembic import command
from psycopg import sql
from sqlalchemy import Engine, create_engine

from tests.support import ScratchDatabase, alembic_config, libpq_url, load_script


@dataclass(frozen=True)
class _Template:
    name: str
    role: str
    password: str


def _admin_execute(admin_url: str, statement: sql.Composed) -> None:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(statement)


def _drop_database(admin_url: str, name: str) -> None:
    _admin_execute(
        admin_url, sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
    )


@pytest.fixture(scope="session")
def admin_url() -> str:
    url = os.environ.get("TEST_ADMIN_DATABASE_URL")
    if not url:
        reason = "TEST_ADMIN_DATABASE_URL is not set: DB-backed tests need a Postgres admin URL"
        if os.environ.get("REQUIRE_DB_TESTS") == "1":
            pytest.fail(f"{reason} (REQUIRE_DB_TESTS=1 forbids skipping them)")
        pytest.skip(reason)
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture(scope="session")
def _template(admin_url: str) -> Iterator[_Template]:
    suffix = secrets.token_hex(4)
    template = _Template(
        name=f"ia_test_{suffix}_tpl", role=f"ia_test_app_{suffix}", password=secrets.token_hex(16)
    )
    _admin_execute(admin_url, sql.SQL("CREATE DATABASE {}").format(sql.Identifier(template.name)))
    try:
        command.upgrade(alembic_config(libpq_url(admin_url, template.name)), "head")
        role_script = load_script("create_app_role")
        # Grants live in the database's catalog, so every clone of the template inherits them.
        with psycopg.connect(libpq_url(admin_url, template.name), autocommit=True) as conn:
            for statement in role_script.build_statements(template.password, role=template.role):
                conn.execute(statement)
        yield template
    finally:
        _drop_database(admin_url, template.name)
        _admin_execute(
            admin_url, sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(template.role))
        )


@pytest.fixture
def test_db(admin_url: str, _template: _Template) -> Iterator[ScratchDatabase]:
    """A fresh, migrated database with the app role's grants, dropped after the test."""
    name = f"ia_test_{secrets.token_hex(6)}"
    _admin_execute(
        admin_url,
        sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
            sql.Identifier(name), sql.Identifier(_template.name)
        ),
    )
    try:
        yield ScratchDatabase(
            url=libpq_url(admin_url, name),
            app_url=libpq_url(admin_url, name, _template.role, _template.password),
        )
    finally:
        _drop_database(admin_url, name)


@pytest.fixture
def empty_db_url(admin_url: str) -> Iterator[str]:
    """An empty database with no migrations applied (libpq form)."""
    name = f"ia_test_{secrets.token_hex(6)}"
    _admin_execute(admin_url, sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield libpq_url(admin_url, name)
    finally:
        _drop_database(admin_url, name)


@pytest.fixture
def owner_engine(test_db: ScratchDatabase) -> Iterator[Engine]:
    engine = create_engine(test_db.sqlalchemy_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def app_engine(test_db: ScratchDatabase) -> Iterator[Engine]:
    engine = create_engine(test_db.app_sqlalchemy_url)
    try:
        yield engine
    finally:
        engine.dispose()
