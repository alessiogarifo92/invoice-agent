"""build_context: the RuleContext assembled from a matcher, the facts and the document."""

import json
from decimal import Decimal

from app.ingestion import extract_document
from app.matching.context import build_context, document_signals
from app.matching.memory import MemoryMatcher
from app.rules.facts import parse_facts
from app.rules.flags import rule_floor
from app.rules.runner import run_rules
from tests.support import FIXTURES, SEED_DIR, SEED_INVOICES
from tests.test_matching import fields, inv


def matcher() -> MemoryMatcher:
    return MemoryMatcher.from_master(json.loads((FIXTURES / "master.json").read_text()))


def test_known_vendor_context() -> None:
    m = matcher()
    m.add_extraction(inv(1), fields(invoice_number="F 2026 307", subtotal="50.00"))
    facts = parse_facts(fields(subtotal="100.00"))
    ctx = build_context(m, facts, invoice_id=inv(2))
    assert ctx.vendor is not None and ctx.vendor.id == "V002"
    assert ctx.vendor_match is not None and ctx.vendor_match.method == "vat_id"
    assert [a.iban for a in ctx.bank_accounts] == ["DE89370400440532013000"]
    assert ctx.po is not None and ctx.po.billed_before == Decimal("50.00")
    assert [(d.invoice_id, d.match) for d in ctx.duplicates] == [(inv(1), "exact")]
    assert ctx.vat_rates["DE"] == Decimal("19.00")
    assert ctx.po_tolerance_pct == Decimal("2.00")
    assert ctx.contract_prices == ()  # V002 has no contract
    assert ctx.document_findings == () and ctx.text_sweep.ibans == ()


def test_the_invoice_itself_is_never_its_own_duplicate_or_billing() -> None:
    m = matcher()
    m.add_extraction(inv(1), fields())
    ctx = build_context(m, parse_facts(fields()), invoice_id=inv(1))
    assert ctx.duplicates == ()
    assert ctx.po is not None and ctx.po.billed_invoice_ids == ()


def test_unknown_vendor_skips_vendor_bound_lookups() -> None:
    m = matcher()
    m.add_extraction(inv(1), fields())
    facts = parse_facts(fields(vendor_vat_id="FR99", vendor_name="Nobody SARL", po_number="X"))
    ctx = build_context(m, facts, invoice_id=inv(2))
    assert ctx.vendor_match is None
    assert ctx.bank_accounts == () and ctx.contract_prices == () and ctx.duplicates == ()
    assert ctx.po is None


def test_contract_prices_need_an_issue_date() -> None:
    facts = parse_facts(fields(vendor_vat_id="IT00000000001", issue_date=None))
    assert build_context(matcher(), facts, invoice_id=inv(2)).contract_prices == ()
    facts = parse_facts(fields(vendor_vat_id="IT00000000001", issue_date="2026-03-01"))
    assert len(build_context(matcher(), facts, invoice_id=inv(2)).contract_prices) == 1


def test_document_signals_sweep_page_text_and_metadata() -> None:
    record = json.loads((SEED_INVOICES / "INV-SEED-005.json").read_text())  # injection_metadata
    doc = extract_document((SEED_INVOICES / record["file"]).read_bytes())
    findings, sweep = document_signals(doc)
    assert {f.kind for f in findings} == {"metadata_instruction"}
    sources = {found.source for found in sweep.ibans if found.value != record["fields"]["iban"]}
    assert sources and all(s.startswith("metadata:") for s in sources)


def test_seed_injection_invoice_end_to_end_is_not_approved() -> None:
    m = MemoryMatcher.from_master_file(SEED_DIR / "master.json")
    record = json.loads((SEED_INVOICES / "INV-SEED-058.json").read_text())  # white text
    doc = extract_document((SEED_INVOICES / record["file"]).read_bytes())
    findings, sweep = document_signals(doc)
    facts = parse_facts(record["fields"])
    flags = run_rules(facts, build_context(m, facts, record["invoice_id"], findings, sweep))
    assert {"injection_suspected", "iban_in_text_not_current"} <= {f.code for f in flags}
    assert rule_floor(flags) == "clarify"
