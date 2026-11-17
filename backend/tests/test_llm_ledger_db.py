"""SqlLedger, the gateway on a real Postgres, and mark_awaiting_quota (restricted app role)."""

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from sqlalchemy import Engine, text

from app.config import Settings
from app.llm.errors import InvalidRequest, QuotaUnavailable
from app.llm.gateway import LLMGateway, mark_awaiting_quota
from app.llm.ledger import LLMCall, SqlLedger
from tests.llm_support import FakeProvider, Invoice

NOW = datetime(2026, 11, 14, 15, 30, tzinfo=UTC)
GOOD = '{"invoice_number": "INV-1", "total": 10}'


def _insert_invoice(engine: Engine, name: str = "a.pdf") -> str:
    with engine.begin() as conn:
        invoice_id: Any = conn.execute(
            text(
                "INSERT INTO invoices (sha256, filename, pdf, size_bytes, page_count) "
                "VALUES (:sha, :name, :pdf, 4, 1) RETURNING id"
            ),
            {"sha": hashlib.sha256(name.encode()).hexdigest(), "name": name, "pdf": b"%PDF"},
        ).scalar_one()
    return str(invoice_id)


def _call(provider: str = "gemini", **kw: Any) -> LLMCall:
    fields: dict[str, Any] = {
        "provider": provider,
        "model": "m",
        "purpose": "extract",
        "status": "ok",
        "created_at": NOW,
    }
    fields.update(kw)
    return LLMCall(**fields)


def _gateway(engine: Engine, primary: FakeProvider, **settings: Any) -> LLMGateway:
    return LLMGateway(
        primary,
        None,
        SqlLedger(engine),
        Settings(_env_file=None, **settings),  # type: ignore[call-arg]
        TracerProvider().get_tracer("t"),
        clock=lambda: NOW,
        sleep=lambda s: None,
    )


def test_record_and_count_by_provider_day_and_token(app_engine: Engine) -> None:
    ledger = SqlLedger(app_engine)
    day = NOW.replace(hour=0, minute=0)
    ledger.record(_call(demo_token_hash="tok"))
    ledger.record(_call(status="error", error_class="RateLimited", demo_token_hash="tok"))
    ledger.record(_call("groq", demo_token_hash="tok"))
    ledger.record(_call(created_at=day - timedelta(microseconds=1)))
    ledger.record(_call(created_at=day + timedelta(days=1)))

    window: dict[str, Any] = {"since": day, "until": day + timedelta(days=1)}
    assert ledger.count_calls(provider="gemini", **window) == 2
    assert ledger.count_calls(provider="groq", **window) == 1
    assert ledger.count_calls(demo_token_hash="tok", **window) == 3
    assert ledger.count_calls(provider="gemini", demo_token_hash="tok", **window) == 2
    assert ledger.count_calls(**window) == 3


def test_record_keeps_every_column(app_engine: Engine, owner_engine: Engine) -> None:
    invoice_id = _insert_invoice(owner_engine)
    SqlLedger(app_engine).record(
        _call(
            input_tokens=12,
            output_tokens=3,
            latency_ms=45,
            status="error",
            error_class="SchemaError",
            invoice_id=invoice_id,
            demo_token_hash="tok",
        )
    )
    with owner_engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM llm_calls")).mappings().one()
    assert str(row["invoice_id"]) == invoice_id
    assert (row["input_tokens"], row["output_tokens"], row["latency_ms"]) == (12, 3, 45)
    assert (row["status"], row["error_class"], row["demo_token_hash"]) == (
        "error",
        "SchemaError",
        "tok",
    )
    assert row["created_at"] == NOW


def test_exhausted_until_is_upserted(app_engine: Engine) -> None:
    ledger = SqlLedger(app_engine)
    assert ledger.exhausted_until("gemini") is None
    ledger.set_exhausted_until("gemini", NOW)
    ledger.set_exhausted_until("gemini", NOW + timedelta(hours=8))
    assert ledger.exhausted_until("gemini") == NOW + timedelta(hours=8)
    assert ledger.exhausted_until("groq") is None


def test_failed_call_row_survives_the_callers_rollback(
    app_engine: Engine, owner_engine: Engine
) -> None:
    invoice_id = _insert_invoice(owner_engine)
    gateway = _gateway(app_engine, FakeProvider("gemini", InvalidRequest("gemini", status=400)))
    with app_engine.connect() as conn, conn.begin() as tx:
        conn.execute(
            text("UPDATE invoices SET status = 'processing' WHERE id = CAST(:id AS uuid)"),
            {"id": invoice_id},
        )
        with pytest.raises(InvalidRequest):
            gateway.complete_structured("extract", "s", "u", Invoice, invoice_id=invoice_id)
        tx.rollback()

    with owner_engine.connect() as conn:
        rows = conn.execute(text("SELECT status, error_class, invoice_id FROM llm_calls")).all()
        status = conn.execute(text("SELECT status FROM invoices")).scalar_one()
    assert [(r.status, r.error_class, str(r.invoice_id)) for r in rows] == [
        ("error", "InvalidRequest", invoice_id)
    ]
    assert status == "received"


def test_soft_cap_counted_from_the_database(app_engine: Engine) -> None:
    ledger = SqlLedger(app_engine)
    for _ in range(2):
        ledger.record(_call())
    ledger.record(_call(created_at=NOW - timedelta(days=1)))
    primary = FakeProvider("gemini", GOOD)
    gateway = _gateway(app_engine, primary, llm_daily_soft_cap_gemini=2)
    with pytest.raises(QuotaUnavailable):
        gateway.complete_structured("extract", "s", "u", Invoice)
    assert primary.calls == []


def test_mark_awaiting_quota_sets_status_and_bumps_version(
    app_engine: Engine, owner_engine: Engine
) -> None:
    invoice_id = _insert_invoice(owner_engine)
    with app_engine.begin() as conn:
        assert mark_awaiting_quota(conn, invoice_id) is True
    with owner_engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, version, updated_at > created_at AS touched FROM invoices")
        ).one()
    assert (row.status, row.version) == ("awaiting_quota", 2)
    assert row.touched


def test_mark_awaiting_quota_on_an_unknown_invoice_returns_false(app_engine: Engine) -> None:
    with app_engine.begin() as conn:
        assert mark_awaiting_quota(conn, "00000000-0000-4000-8000-000000000000") is False
