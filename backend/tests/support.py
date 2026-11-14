"""Helpers shared by the test modules and conftest (import from here, not from conftest)."""

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from alembic.config import Config
from sqlalchemy import make_url

from app.db import normalize_url

BACKEND = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_script(name: str) -> ModuleType:
    """Import a module from backend/scripts (not a package) by file path."""
    spec = importlib.util.spec_from_file_location(name, BACKEND / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def alembic_config(url: str) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    # Read by alembic/env.py in place of the settings, so tests never migrate a real DB.
    cfg.attributes["database_url"] = url
    return cfg


def libpq_url(admin_url: str, database: str, user: str | None = None, pw: str | None = None) -> str:
    url = make_url(admin_url).set(drivername="postgresql", database=database)
    if user is not None:
        url = url.set(username=user, password=pw)
    return url.render_as_string(hide_password=False)


@dataclass(frozen=True)
class ScratchDatabase:
    url: str  # libpq form, for psycopg
    app_url: str  # same database, connecting as the restricted test app role

    @property
    def sqlalchemy_url(self) -> str:
        return normalize_url(self.url)

    @property
    def app_sqlalchemy_url(self) -> str:
        return normalize_url(self.app_url)


SEED_DIR = BACKEND.parent / "data" / "seed"
SEED_INVOICES = SEED_DIR / "invoices"
