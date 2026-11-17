"""Helpers shared by the test modules and conftest (import from here, not from conftest)."""

import hashlib
import importlib.util
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from alembic.config import Config
from sqlalchemy import Connection, make_url, text

from app.db import normalize_url
from app.matching.base import extraction_record

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


def insert_extraction(
    conn: Connection, invoice_id: str, fields: dict[str, Any], created_at: datetime
) -> str:
    """An invoice (once), a processing run and one extraction row for it; returns the run id.

    The searchable columns come from `extraction_record`, as the extraction writer stores them.
    """
    record = extraction_record(fields)
    conn.execute(
        text(
            "INSERT INTO invoices (id, sha256, filename, pdf, size_bytes, page_count) "
            "VALUES (:id, :sha, :name, :pdf, 4, 1) ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": invoice_id,
            "sha": hashlib.sha256(invoice_id.encode()).hexdigest(),
            "name": f"{invoice_id}.pdf",
            "pdf": b"%PDF",
        },
    )
    run_id = str(
        conn.execute(
            text(
                "INSERT INTO processing_runs (invoice_id, provider, extraction_model, "
                "prompt_version) VALUES (:id, 'test', 'test', 'test') RETURNING id"
            ),
            {"id": invoice_id},
        ).scalar_one()
    )
    conn.execute(
        text(
            "INSERT INTO extractions (invoice_id, run_id, fields, vendor_vat_id, invoice_number, "
            "invoice_number_norm, total, issue_date, created_at) VALUES (:invoice_id, :run_id, "
            "CAST(:fields AS jsonb), :vat, :number, :norm, :total, :issue_date, :created_at)"
        ),
        {
            "invoice_id": invoice_id,
            "run_id": run_id,
            "fields": json.dumps(fields),
            "vat": record.vendor_vat_id,
            "number": record.invoice_number,
            "norm": record.invoice_number_norm,
            "total": record.total,
            "issue_date": record.issue_date,
            "created_at": created_at,
        },
    )
    return run_id
