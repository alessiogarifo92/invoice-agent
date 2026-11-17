"""Migrations round-trip on an empty database, and the 0002 schema's constraints."""

import uuid

import psycopg
import pytest
from alembic import command
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import IntegrityError

from tests.support import ScratchDatabase, alembic_config

MASTER_TABLES = {
    "vendors",
    "vendor_bank_accounts",
    "purchase_orders",
    "po_lines",
    "contracts",
    "contract_prices",
    "contract_clauses",
    "vat_rates",
    "settings_kv",
}
PROCESSING_TABLES = {
    "invoices",
    "processing_runs",
    "extractions",
    "flags",
    "proposals",
    "approvals",
    "llm_calls",
    "jobs",
    "budget_state",
}
SCHEMA_0002 = MASTER_TABLES | PROCESSING_TABLES | {"audit_events"}


def _tables(url: str) -> set[str]:
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
    return {r[0] for r in rows}


def _functions(url: str) -> set[str]:
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.prokind = 'f' "
            "AND NOT EXISTS (SELECT FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')"
        ).fetchall()
    return {r[0] for r in rows}


def test_upgrade_downgrade_upgrade_on_an_empty_database(empty_db_url: str) -> None:
    cfg = alembic_config(empty_db_url)

    command.upgrade(cfg, "head")
    assert _tables(empty_db_url) == SCHEMA_0002 | {"skeleton_probe", "alembic_version"}
    assert _functions(empty_db_url) == {"audit_events_reject_mutation"}

    command.downgrade(cfg, "base")
    assert _tables(empty_db_url) == {"alembic_version"}
    assert _functions(empty_db_url) == set()

    command.upgrade(cfg, "head")
    assert _tables(empty_db_url) == SCHEMA_0002 | {"skeleton_probe", "alembic_version"}


def test_downgrade_to_0001_keeps_the_skeleton(empty_db_url: str) -> None:
    cfg = alembic_config(empty_db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0001")
    assert _tables(empty_db_url) == {"skeleton_probe", "alembic_version"}


def _invoice(conn: Connection, status: str = "received") -> uuid.UUID:
    invoice_id: uuid.UUID = conn.execute(
        text(
            "INSERT INTO invoices (sha256, filename, pdf, size_bytes, page_count, status) "
            "VALUES (:sha, 'a.pdf', '\\x255044462d', 5, 1, :status) RETURNING id"
        ),
        {"sha": uuid.uuid4().hex * 2, "status": status},
    ).scalar_one()
    return invoice_id


def _run(conn: Connection, invoice_id: uuid.UUID) -> uuid.UUID:
    run_id: uuid.UUID = conn.execute(
        text(
            "INSERT INTO processing_runs (invoice_id, provider, extraction_model, prompt_version) "
            "VALUES (:inv, 'gemini', 'flash-lite', 'p1') RETURNING id"
        ),
        {"inv": invoice_id},
    ).scalar_one()
    return run_id


def test_new_invoice_defaults(owner_engine: Engine) -> None:
    with owner_engine.begin() as conn:
        invoice_id = _invoice(conn)
        status, version, created, updated = conn.execute(
            text("SELECT status, version, created_at, updated_at FROM invoices WHERE id = :id"),
            {"id": invoice_id},
        ).one()
    assert (status, version) == ("received", 1)
    assert created == updated


@pytest.mark.parametrize(
    "status",
    [
        "received",
        "processing",
        "pending_approval",
        "approved_for_payment",
        "clarification_requested",
        "blocked",
        "awaiting_quota",
        "failed",
    ],
)
def test_invoice_accepts_every_documented_status(owner_engine: Engine, status: str) -> None:
    with owner_engine.begin() as conn:
        _invoice(conn, status)


def test_invoice_rejects_an_unknown_status(owner_engine: Engine) -> None:
    with owner_engine.begin() as conn, pytest.raises(IntegrityError, match="invoices_status"):
        _invoice(conn, "paid")


def test_invoice_sha256_is_unique(owner_engine: Engine) -> None:
    sql = (
        "INSERT INTO invoices (sha256, filename, pdf, size_bytes, page_count) "
        "VALUES (:sha, 'a.pdf', '\\x00', 1, 1)"
    )
    with owner_engine.begin() as conn:
        conn.execute(text(sql), {"sha": "a" * 64})
        with pytest.raises(IntegrityError, match="invoices_sha256"):
            conn.execute(text(sql), {"sha": "a" * 64})


def test_invoice_sha256_must_be_lowercase_hex(owner_engine: Engine) -> None:
    with owner_engine.begin() as conn, pytest.raises(IntegrityError, match="invoices_sha256"):
        conn.execute(
            text(
                "INSERT INTO invoices (sha256, filename, pdf, size_bytes, page_count) "
                "VALUES ('not-a-hash', 'a.pdf', '\\x00', 1, 1)"
            )
        )


def test_extractions_allow_duplicates_for_the_duplicate_search(owner_engine: Engine) -> None:
    with owner_engine.begin() as conn:
        for _ in range(2):
            invoice_id = _invoice(conn)
            conn.execute(
                text(
                    "INSERT INTO extractions (invoice_id, run_id, fields, vendor_vat_id, "
                    "invoice_number, invoice_number_norm, total, issue_date) VALUES "
                    "(:inv, :run, '{}', 'IT01', 'INV-1', 'INV1', 100.00, '2026-01-10')"
                ),
                {"inv": invoice_id, "run": _run(conn, invoice_id)},
            )
        similar = conn.execute(
            text(
                "SELECT count(*) FROM extractions "
                "WHERE invoice_number_norm % 'INV-1' AND vendor_vat_id = 'IT01'"
            )
        ).scalar_one()
    assert similar == 2


@pytest.mark.parametrize(
    ("column", "value"),
    [("rule_floor", "pay"), ("agent_action", "pay"), ("final_action", "pay")],
)
def test_proposal_actions_are_constrained(owner_engine: Engine, column: str, value: str) -> None:
    actions = {"rule_floor": "approve", "agent_action": "approve", "final_action": "approve"}
    actions[column] = value
    with owner_engine.begin() as conn:
        invoice_id = _invoice(conn)
        run_id = _run(conn, invoice_id)
        with pytest.raises(IntegrityError, match="proposals_"):
            conn.execute(
                text(
                    "INSERT INTO proposals (invoice_id, run_id, rule_floor, agent_action, "
                    "final_action, rationale) VALUES (:inv, :run, :rf, :aa, :fa, '')"
                ),
                {
                    "inv": invoice_id,
                    "run": run_id,
                    "rf": actions["rule_floor"],
                    "aa": actions["agent_action"],
                    "fa": actions["final_action"],
                },
            )


def test_proposal_without_agent_and_its_approval(owner_engine: Engine) -> None:
    with owner_engine.begin() as conn:
        invoice_id = _invoice(conn)
        proposal_id = conn.execute(
            text(
                "INSERT INTO proposals (invoice_id, run_id, rule_floor, final_action, rationale) "
                "VALUES (:inv, :run, 'approve', 'approve', 'all rules passed') RETURNING id"
            ),
            {"inv": invoice_id, "run": _run(conn, invoice_id)},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO approvals (proposal_id, decision, decided_by) "
                "VALUES (:p, 'approve', 'reviewer')"
            ),
            {"p": proposal_id},
        )
        with pytest.raises(IntegrityError, match="approvals_decision"):
            conn.execute(
                text(
                    "INSERT INTO approvals (proposal_id, decision, decided_by) "
                    "VALUES (:p, 'pay', 'reviewer')"
                ),
                {"p": proposal_id},
            )


def test_contract_clauses_are_full_text_searchable(owner_engine: Engine) -> None:
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO vendors (id, name, vat_id, country, address) "
                "VALUES ('V001', 'Acme', 'IT01', 'IT', 'Via Roma 1')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO contracts (id, vendor_id, currency, valid_from, valid_to) "
                "VALUES ('C001', 'V001', 'EUR', '2025-01-01', '2026-12-31')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO contract_clauses (contract_id, clause_no, title, text) VALUES "
                "('C001', '4.2', 'Late delivery', 'Penalties apply when shipments arrive late.'),"
                "('C001', '5.1', 'Payment terms', 'Invoices are paid within 60 days.')"
            )
        )
        hits = conn.execute(
            text(
                "SELECT clause_no FROM contract_clauses "
                "WHERE tsv @@ websearch_to_tsquery('english', 'penalty shipment')"
            )
        ).scalars()
        assert list(hits) == ["4.2"]


def test_search_indexes_exist(test_db: ScratchDatabase) -> None:
    with psycopg.connect(test_db.url) as conn:
        indexes = {
            name: definition
            for name, definition in conn.execute(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'"
            ).fetchall()
        }
    defs = " ".join(indexes.values())
    assert "ON public.vendors USING gin (name gin_trgm_ops)" in defs
    assert "ON public.extractions USING gin (invoice_number_norm gin_trgm_ops)" in defs
    assert "ON public.contract_clauses USING gin (tsv)" in defs
    assert "ON public.llm_calls USING btree (provider, created_at)" in defs


def _job(conn: Connection, invoice_id: uuid.UUID, status: str = "queued") -> None:
    conn.execute(
        text("INSERT INTO jobs (invoice_id, stage, status) VALUES (:inv, 'extract', :status)"),
        {"inv": invoice_id, "status": status},
    )


@pytest.mark.parametrize("active", ["queued", "running"])
def test_an_invoice_cannot_have_two_active_jobs(owner_engine: Engine, active: str) -> None:
    with owner_engine.begin() as conn:
        invoice_id = _invoice(conn)
        _job(conn, invoice_id, active)
        with pytest.raises(IntegrityError, match="jobs_one_active_per_invoice"):
            _job(conn, invoice_id, "queued")


def test_a_finished_job_does_not_block_a_new_one(owner_engine: Engine) -> None:
    with owner_engine.begin() as conn:
        invoice_id = _invoice(conn)
        _job(conn, invoice_id, "queued")
        conn.execute(
            text("UPDATE jobs SET status = 'done' WHERE invoice_id = :inv"), {"inv": invoice_id}
        )
        _job(conn, invoice_id, "failed")
        _job(conn, invoice_id, "queued")
        count = conn.execute(
            text("SELECT count(*) FROM jobs WHERE invoice_id = :inv"), {"inv": invoice_id}
        ).scalar_one()
    assert count == 3


def test_job_status_is_constrained(owner_engine: Engine) -> None:
    with owner_engine.begin() as conn:
        invoice_id = _invoice(conn)
        with pytest.raises(IntegrityError, match="jobs_status"):
            _job(conn, invoice_id, "paused")


def test_llm_call_without_an_invoice(owner_engine: Engine) -> None:
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO llm_calls (provider, model, purpose, input_tokens, output_tokens, "
                "latency_ms, status) VALUES ('gemini', 'flash', 'eval', 10, 5, 120, 'ok')"
            )
        )
        conn.execute(text("INSERT INTO budget_state (provider) VALUES ('gemini')"))
