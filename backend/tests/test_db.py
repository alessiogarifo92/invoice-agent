"""app.db without a database: URL normalisation and lazy engine creation."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_engine, get_sessionmaker, normalize_url
from app.main import create_app


@pytest.fixture
def unreachable_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Port 1 on localhost: nothing listens there, so any connection attempt would fail.
    monkeypatch.setenv("DATABASE_URL", "postgresql://app:x@127.0.0.1:1/none")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


def test_normalize_url_adds_the_psycopg_driver() -> None:
    assert normalize_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"


def test_normalize_url_keeps_an_explicit_driver() -> None:
    assert normalize_url("postgresql+psycopg://u:p@h/db") == "postgresql+psycopg://u:p@h/db"


@pytest.mark.usefixtures("unreachable_db")
def test_healthz_never_creates_the_engine() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/healthz").status_code == 200
    assert get_engine.cache_info().currsize == 0


@pytest.mark.usefixtures("unreachable_db")
def test_engine_is_built_from_settings_with_pre_ping() -> None:
    engine = get_engine()
    assert engine.url.drivername == "postgresql+psycopg"
    assert engine.url.port == 1
    assert engine.pool._pre_ping is True
    assert get_engine() is engine
    assert get_sessionmaker().kw["bind"] is engine
