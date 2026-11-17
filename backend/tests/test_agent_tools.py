"""The agent's tools on the seed master data: bound to the invoice, read-only, each result
carrying a run-scoped evidence ref."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app.agent.tools import PROPOSE, TOOL_SPECS, InvoiceTools, load_scope, read_only
from app.llm.provider import InvalidToolArguments
from tests.extraction_support import seed_fields
from tests.jobs_support import load_seed_master
from tests.support import ScratchDatabase, insert_extraction

T0 = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture
def seeded(test_db: ScratchDatabase) -> ScratchDatabase:
    load_seed_master(test_db)
    return test_db


def _store(owner_engine: Engine, seed_id: str, at: datetime = T0, **changes: Any) -> str:
    invoice_id = str(uuid.uuid4())
    with owner_engine.begin() as conn:
        insert_extraction(conn, invoice_id, {**seed_fields(seed_id), **changes}, at)
    return invoice_id


def _tools(app_engine: Engine, owner_engine: Engine, seed_id: str, **changes: Any) -> InvoiceTools:
    invoice_id = _store(owner_engine, seed_id, **changes)
    with app_engine.connect() as conn:
        scope = load_scope(conn, invoice_id, {**seed_fields(seed_id), **changes})
    return InvoiceTools(app_engine, scope, ref_tag="t1")


def _call(tools: InvoiceTools, name: str, **arguments: Any) -> Any:
    return tools.handlers[name](arguments)


def test_every_spec_is_a_closed_object_and_only_the_search_takes_arguments() -> None:
    for spec in TOOL_SPECS:
        assert spec.parameters["type"] == "object"
        assert spec.parameters["additionalProperties"] is False
        assert set(spec.parameters["required"]) == set(spec.parameters["properties"])
    with_arguments = {s.name for s in TOOL_SPECS if s.parameters["properties"]}
    assert with_arguments == {"search_contract_clauses", PROPOSE}


def test_get_vendor_reports_an_iban_that_is_not_the_current_account(
    seeded: ScratchDatabase, app_engine: Engine, owner_engine: Engine
) -> None:
    tools = _tools(app_engine, owner_engine, "INV-SEED-037")  # iban_swap
    output = _call(tools, "get_vendor")

    assert output["evidence_ref"] == "ev-t1-1"
    assert output["found"] is True and output["vendor"]["id"] == "V002"
    assert output["match_method"] == "vat_id"
    assert output["invoice_iban"] == "DE84ZZZZ26304761683594"
    assert output["invoice_iban_status"] == "never_seen"
    assert [a["current"] for a in output["bank_accounts"]].count(True) == 1
    (evidence,) = tools.evidence
    assert (evidence.ref, evidence.tool, evidence.records) == ("ev-t1-1", "get_vendor", ("V002",))


def test_an_unknown_vendor_has_no_master_data_and_no_scope(
    seeded: ScratchDatabase, app_engine: Engine, owner_engine: Engine
) -> None:
    tools = _tools(app_engine, owner_engine, "INV-SEED-040")  # unknown_vendor
    assert _call(tools, "get_vendor") == {"evidence_ref": "ev-t1-1", "found": False}
    similar = _call(tools, "find_similar_invoices")
    assert similar["vendor_known"] is False and similar["candidates"] == []
    clauses = _call(tools, "search_contract_clauses", query="bank account")
    assert clauses["vendor_known"] is False and clauses["clauses"] == []
    po = _call(tools, "get_purchase_order")
    assert po["found"] is False and po["reason"] == "not_in_master_data"
    assert [e.records for e in tools.evidence] == [(), (), (), ()]


def test_get_purchase_order_returns_the_printed_po_with_its_balance(
    seeded: ScratchDatabase, app_engine: Engine, owner_engine: Engine
) -> None:
    tools = _tools(app_engine, owner_engine, "INV-SEED-024")
    output = _call(tools, "get_purchase_order")

    assert output["id"] == "PO-2026-0022" and output["belongs_to_invoice_vendor"] is True
    assert output["lines"] and all(isinstance(line["unit_price"], str) for line in output["lines"])
    assert output["billed_invoice_ids"] == []
    assert output["remaining_net"] == output["total_net"]
    assert output["this_invoice_net"] == seed_fields("INV-SEED-024")["subtotal"]
    assert tools.evidence[0].records == ("PO-2026-0022",)


def test_a_po_of_another_vendor_is_confirmed_but_not_opened(
    seeded: ScratchDatabase, app_engine: Engine, owner_engine: Engine
) -> None:
    # INV-SEED-037 is V002's; PO-2026-0001 belongs to V009.
    tools = _tools(app_engine, owner_engine, "INV-SEED-037", po_number="PO-2026-0001")
    output = _call(tools, "get_purchase_order")
    assert output == {
        "evidence_ref": "ev-t1-1",
        "found": True,
        "id": "PO-2026-0001",
        "belongs_to_invoice_vendor": False,
    }


def test_find_similar_invoices_finds_the_original_of_a_resend_but_never_itself(
    seeded: ScratchDatabase, app_engine: Engine, owner_engine: Engine
) -> None:
    original = _store(owner_engine, "INV-SEED-024", T0 - timedelta(days=3))
    tools = _tools(app_engine, owner_engine, "INV-SEED-063")  # "RE 356" resends "RE-00356"
    output = _call(tools, "find_similar_invoices")

    (candidate,) = output["candidates"]
    assert candidate["invoice_id"] == original and candidate["match"] == "exact"
    assert candidate["total"] == "823.60"
    assert tools.evidence[0].records == (original,)
    assert tools.scope.invoice_id not in tools.evidence[0].records


def test_check_vat_recomputes_with_the_rules(
    seeded: ScratchDatabase, app_engine: Engine, owner_engine: Engine
) -> None:
    tools = _tools(app_engine, owner_engine, "INV-SEED-044")  # wrong_vat
    output = _call(tools, "check_vat")

    assert output["country_source"] == "master_data"
    assert output["expected_rate_pct"] != output["invoice_rate_pct"]
    assert "wrong_vat" in {f["code"] for f in output["findings"]}
    assert tools.evidence[0].records == (f"vat_rates/{output['country']}",)
    clean = _call(_tools(app_engine, owner_engine, "INV-SEED-001"), "check_vat")
    assert clean["findings"] == []


def test_contract_clauses_are_searched_within_the_invoices_vendor(
    seeded: ScratchDatabase, app_engine: Engine, owner_engine: Engine
) -> None:
    tools = _tools(app_engine, owner_engine, "INV-SEED-023")  # V001, a clean invoice
    output = _call(tools, "search_contract_clauses", query="change of bank details")
    assert [(c["contract_id"], c["clause_no"]) for c in output["clauses"]] == [("C001", "5.1")]
    assert tools.evidence[0].records == ("C001/5.1",)
    # Every seed vendor has the same clauses: V002's invoice only ever sees V002's contract.
    other = _tools(app_engine, owner_engine, "INV-SEED-037")
    hits = _call(other, "search_contract_clauses", query="bank details payment")["clauses"]
    assert hits and {c["contract_id"] for c in hits} == {"C002"}


@pytest.mark.parametrize(
    ("name", "arguments", "problem"),
    [
        ("get_vendor", {"vendor_id": "V001"}, ("vendor_id", "extra_forbidden")),
        ("get_purchase_order", {"po_id": "PO-2026-0001"}, ("po_id", "extra_forbidden")),
        ("find_similar_invoices", {"invoice_id": "x"}, ("invoice_id", "extra_forbidden")),
        ("check_vat", {"country": "IE"}, ("country", "extra_forbidden")),
        (
            "search_contract_clauses",
            {"query": "bank", "vendor_id": "V001"},
            ("vendor_id", "extra_forbidden"),
        ),
        ("search_contract_clauses", {}, ("query", "missing")),
        ("search_contract_clauses", {"query": 7}, ("query", "string_type")),
        ("search_contract_clauses", {"query": " \x00 "}, ("query", "value_error")),
    ],
)
def test_foreign_ids_and_malformed_arguments_are_rejected_not_followed(
    seeded: ScratchDatabase,
    app_engine: Engine,
    owner_engine: Engine,
    name: str,
    arguments: dict[str, Any],
    problem: tuple[str, str],
) -> None:
    tools = _tools(app_engine, owner_engine, "INV-SEED-037")
    with pytest.raises(InvalidToolArguments) as info:
        _call(tools, name, **arguments)
    assert [(p["loc"], p["type"]) for p in info.value.problems] == [problem]
    # Nothing was looked up, so there is no evidence to cite.
    assert tools.evidence == []


def test_evidence_refs_count_up_within_the_run_and_differ_between_runs(
    seeded: ScratchDatabase, app_engine: Engine, owner_engine: Engine
) -> None:
    tools = _tools(app_engine, owner_engine, "INV-SEED-037")
    refs = [_call(tools, n)["evidence_ref"] for n in ("get_vendor", "check_vat", "get_vendor")]
    assert refs == ["ev-t1-1", "ev-t1-2", "ev-t1-3"]
    assert tools.returned_refs == frozenset(refs)
    a = InvoiceTools(app_engine, tools.scope)
    b = InvoiceTools(app_engine, tools.scope)
    assert _call(a, "get_vendor")["evidence_ref"] != _call(b, "get_vendor")["evidence_ref"]


def test_tool_connections_refuse_writes(seeded: ScratchDatabase, owner_engine: Engine) -> None:
    # The owner role may write anywhere: only the READ ONLY transaction stops it here.
    with pytest.raises(DBAPIError, match="read-only transaction"):
        with read_only(owner_engine) as conn:
            conn.execute(text("UPDATE vendors SET name = 'x'"))
