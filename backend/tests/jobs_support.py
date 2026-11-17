"""Helpers for the queue, pipeline and worker tests: seed uploads and a fake-backed gateway.

The gateway writes to the real ledger (llm_calls on the scratch database), so a test can
assert how many provider requests a job made. The provider is always a FakeProvider: no
test here ever reaches a live API.
"""

import hashlib
from typing import Any
from uuid import UUID

import psycopg
from opentelemetry.sdk.trace import TracerProvider
from sqlalchemy import Engine, text

from app.config import Settings
from app.ingestion import store
from app.ingestion.upload import inspect_pdf
from app.llm.gateway import LLMGateway
from app.llm.ledger import SqlLedger
from tests.llm_support import FAKE_KEY, FakeProvider
from tests.support import SEED_DIR, SEED_INVOICES, ScratchDatabase, load_script

loader = load_script("load_master_data")


def make_settings(**overrides: Any) -> Settings:
    """Settings that ignore any local .env, with a fake Gemini key unless overridden."""
    values: dict[str, Any] = {"gemini_api_key": FAKE_KEY, **overrides}
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def sql_gateway(engine: Engine, provider: FakeProvider, **settings: Any) -> LLMGateway:
    return LLMGateway(
        provider,
        None,
        SqlLedger(engine),
        make_settings(**settings),
        TracerProvider().get_tracer("test"),
        sleep=lambda s: None,
    )


def load_seed_master(test_db: ScratchDatabase) -> None:
    with psycopg.connect(test_db.url) as conn:
        loader.load(conn, loader.read_master(SEED_DIR / "master.json"))


def upload_seed(engine: Engine, seed_id: str) -> UUID:
    """Store a seed PDF as POST /invoices does: the invoice, its queued job, the audit event."""
    data = (SEED_INVOICES / f"{seed_id}.pdf").read_bytes()
    with engine.connect() as conn:
        record, created = store.store_invoice(
            conn, data, hashlib.sha256(data).hexdigest(), f"{seed_id}.pdf", inspect_pdf(data, 5)
        )
    assert created
    return record.id


def insert_invoice(engine: Engine, name: str) -> UUID:
    """A bare invoice row with no job (the PDF bytes are a stub)."""
    with engine.begin() as conn:
        invoice_id: UUID = conn.execute(
            text(
                "INSERT INTO invoices (sha256, filename, pdf, size_bytes, page_count) "
                "VALUES (:sha, :name, :pdf, 4, 1) RETURNING id"
            ),
            {"sha": hashlib.sha256(name.encode()).hexdigest(), "name": name, "pdf": b"%PDF"},
        ).scalar_one()
    return invoice_id


def scalar(engine: Engine, sql: str, **params: Any) -> Any:
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


def rows(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql), params).all())


def llm_calls(engine: Engine, invoice_id: UUID) -> int:
    count: int = scalar(
        engine, "SELECT count(*) FROM llm_calls WHERE invoice_id = :id", id=invoice_id
    )
    return count


def event_types(engine: Engine, invoice_id: UUID) -> list[str]:
    return [
        r.event_type
        for r in rows(
            engine,
            "SELECT event_type FROM audit_events WHERE entity_id = :id ORDER BY seq",
            id=str(invoice_id),
        )
    ]
