"""persist_extraction on a real Postgres, as the restricted app role."""

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, text

from app.extraction.prompt import PROMPT_VERSION
from app.extraction.service import (
    extract_invoice,
    find_stored_extraction,
    load_stored_extraction,
    persist_extraction,
)
from tests.extraction_support import EXTRACT_MODEL, fake_gateway, seed_document, seed_fields
from tests.llm_support import FakeProvider


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


def _outcome(reply: str, seed: str = "INV-SEED-001", invoice_id: str = "x") -> Any:
    provider = FakeProvider("gemini", reply)
    return extract_invoice(fake_gateway(provider), seed_document(seed), invoice_id)


def _rows(engine: Engine, sql: str, invoice_id: str) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql), {"id": invoice_id}).mappings())


def test_a_clean_extraction_writes_the_run_and_the_indexed_columns(
    app_engine: Engine, owner_engine: Engine
) -> None:
    # INV-SEED-063 is the resend whose number is printed "RE 356".
    invoice_id = _insert_invoice(owner_engine)
    fields = seed_fields("INV-SEED-063")
    outcome = _outcome(json.dumps(fields), "INV-SEED-063", invoice_id)
    with app_engine.begin() as conn:
        run_id = persist_extraction(conn, invoice_id, outcome)

    (run,) = _rows(app_engine, "SELECT * FROM processing_runs WHERE invoice_id = :id", invoice_id)
    assert str(run["id"]) == run_id
    assert (run["provider"], run["extraction_model"], run["prompt_version"]) == (
        "gemini",
        EXTRACT_MODEL,
        PROMPT_VERSION,
    )
    assert run["agent_model"] is None and run["finished_at"] is None

    (row,) = _rows(app_engine, "SELECT * FROM extractions WHERE invoice_id = :id", invoice_id)
    assert str(row["run_id"]) == run_id
    assert row["fields"] == fields
    assert row["vendor_vat_id"] == fields["vendor_vat_id"]
    assert row["invoice_number"] == "RE 356"
    assert row["invoice_number_norm"] == "RE356"
    assert row["total"] == Decimal(fields["total"])
    assert row["issue_date"] == date.fromisoformat(fields["issue_date"])
    assert _rows(app_engine, "SELECT * FROM flags WHERE invoice_id = :id", invoice_id) == []


def test_the_searchable_columns_are_stored_as_the_duplicate_search_compares_them(
    app_engine: Engine, owner_engine: Engine
) -> None:
    # The duplicate search joins on the master-data VAT id, which is compacted.
    invoice_id = _insert_invoice(owner_engine)
    fields = seed_fields("INV-SEED-001")
    spaced = " ".join(fields["vendor_vat_id"][i : i + 4] for i in range(0, 13, 4)).strip()
    outcome = _outcome(json.dumps({**fields, "vendor_vat_id": spaced}), invoice_id=invoice_id)
    assert outcome.extraction.vendor_vat_id == spaced
    with app_engine.begin() as conn:
        persist_extraction(conn, invoice_id, outcome)

    (row,) = _rows(app_engine, "SELECT * FROM extractions WHERE invoice_id = :id", invoice_id)
    assert row["vendor_vat_id"] == fields["vendor_vat_id"]
    assert row["fields"]["vendor_vat_id"] == spaced


def test_a_stored_extraction_is_found_by_invoice_and_prompt_version(
    app_engine: Engine, owner_engine: Engine
) -> None:
    invoice_id = _insert_invoice(owner_engine)
    fields = {**seed_fields("INV-SEED-001"), "iban": "NL67XZXZ3387385475"}
    outcome = _outcome(json.dumps(fields), invoice_id=invoice_id)
    with app_engine.begin() as conn:
        assert find_stored_extraction(conn, invoice_id, PROMPT_VERSION) is None
        run_id = persist_extraction(conn, invoice_id, outcome)
    with app_engine.connect() as conn:
        stored = find_stored_extraction(conn, invoice_id, PROMPT_VERSION)
        assert find_stored_extraction(conn, invoice_id, "extract-v0") is None
        assert stored == load_stored_extraction(conn, run_id)
    assert stored is not None
    assert (stored.run_id, stored.provider, stored.model) == (run_id, "gemini", EXTRACT_MODEL)
    assert stored.fields == fields
    assert [(f.code, f.severity, f.action_floor) for f in stored.flags] == [
        ("grounding_failure", "warning", "clarify")
    ]
    assert stored.action_floor == "clarify"


def test_a_stored_schema_failure_has_no_fields(app_engine: Engine, owner_engine: Engine) -> None:
    invoice_id = _insert_invoice(owner_engine)
    with app_engine.begin() as conn:
        run_id = persist_extraction(conn, invoice_id, _outcome("nope", invoice_id=invoice_id))
        stored = load_stored_extraction(conn, run_id)
    assert stored.fields is None
    assert [f.code for f in stored.flags] == ["schema_failure"]


def test_a_clean_stored_extraction_floors_at_approve(
    app_engine: Engine, owner_engine: Engine
) -> None:
    invoice_id = _insert_invoice(owner_engine)
    outcome = _outcome(json.dumps(seed_fields("INV-SEED-001")), invoice_id=invoice_id)
    with app_engine.begin() as conn:
        stored = load_stored_extraction(conn, persist_extraction(conn, invoice_id, outcome))
    assert stored.flags == () and stored.action_floor == "approve"


def test_grounding_failures_become_flag_rows_of_the_run(
    app_engine: Engine, owner_engine: Engine
) -> None:
    invoice_id = _insert_invoice(owner_engine)
    fields = seed_fields("INV-SEED-001")
    fields["iban"] = "NL67XZXZ3387385475"
    outcome = _outcome(json.dumps(fields), invoice_id=invoice_id)
    with app_engine.begin() as conn:
        run_id = persist_extraction(conn, invoice_id, outcome)

    (flag,) = _rows(app_engine, "SELECT * FROM flags WHERE invoice_id = :id", invoice_id)
    assert (str(flag["run_id"]), flag["code"], flag["severity"]) == (
        run_id,
        "grounding_failure",
        "warning",
    )
    assert flag["evidence"] == {"field": "iban", "value": "NL67XZXZ3387385475"}
    # The extraction is kept as returned: the flag, not a silent edit, carries the doubt.
    (row,) = _rows(app_engine, "SELECT fields FROM extractions WHERE invoice_id = :id", invoice_id)
    assert row["fields"]["iban"] == "NL67XZXZ3387385475"


def test_a_schema_failure_writes_the_run_and_its_flag_but_no_extraction(
    app_engine: Engine, owner_engine: Engine
) -> None:
    invoice_id = _insert_invoice(owner_engine)
    outcome = _outcome("not json at all", invoice_id=invoice_id)
    with app_engine.begin() as conn:
        persist_extraction(conn, invoice_id, outcome)

    (run,) = _rows(app_engine, "SELECT * FROM processing_runs WHERE invoice_id = :id", invoice_id)
    assert run["extraction_model"] == EXTRACT_MODEL
    assert _rows(app_engine, "SELECT * FROM extractions WHERE invoice_id = :id", invoice_id) == []
    (flag,) = _rows(app_engine, "SELECT * FROM flags WHERE invoice_id = :id", invoice_id)
    assert flag["code"] == "schema_failure"


def test_null_indexed_values_are_stored_as_null(app_engine: Engine, owner_engine: Engine) -> None:
    invoice_id = _insert_invoice(owner_engine)
    fields = {**seed_fields("INV-SEED-001"), "invoice_number": None, "total": None}
    fields["issue_date"] = None
    outcome = _outcome(json.dumps(fields), invoice_id=invoice_id)
    with app_engine.begin() as conn:
        persist_extraction(conn, invoice_id, outcome)

    (row,) = _rows(app_engine, "SELECT * FROM extractions WHERE invoice_id = :id", invoice_id)
    assert row["invoice_number"] is None and row["invoice_number_norm"] is None
    assert row["total"] is None and row["issue_date"] is None


def test_persist_runs_in_the_callers_transaction(app_engine: Engine, owner_engine: Engine) -> None:
    invoice_id = _insert_invoice(owner_engine)
    outcome = _outcome(json.dumps(seed_fields("INV-SEED-001")), invoice_id=invoice_id)
    with app_engine.connect() as conn:
        persist_extraction(conn, invoice_id, outcome)
        conn.rollback()
    assert (
        _rows(app_engine, "SELECT id FROM processing_runs WHERE invoice_id = :id", invoice_id) == []
    )
