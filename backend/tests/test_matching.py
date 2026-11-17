"""The matching contract, run against both implementations: in-memory and SQL.

The SQL variant runs on a fresh scratch database with the small master fixture loaded by
the real loader, and queries it as the restricted app role.
"""

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

import psycopg
import pytest
from sqlalchemy import Connection, Engine

from app.matching.base import Matcher, extraction_record
from app.matching.memory import MemoryMatcher
from app.matching.sql import SqlMatcher
from app.rules.context import BankAccountRecord, ContractPriceRecord, POLineRecord
from app.rules.numbers import trigram_similarity
from tests.support import FIXTURES, ScratchDatabase, insert_extraction, load_script

FIXTURE = FIXTURES / "master.json"
loader = load_script("load_master_data")
T0 = datetime(2026, 3, 1, tzinfo=UTC)


class Harness(Protocol):
    @property
    def matcher(self) -> Matcher: ...

    def add_extraction(self, invoice_id: str, fields: dict[str, Any]) -> None: ...


@dataclass
class MemoryHarness:
    matcher: MemoryMatcher = field(
        default_factory=lambda: MemoryMatcher.from_master(json.loads(FIXTURE.read_text()))
    )

    def add_extraction(self, invoice_id: str, fields: dict[str, Any]) -> None:
        self.matcher.add_extraction(invoice_id, fields)


@dataclass
class SqlHarness:
    conn: Connection
    matcher: SqlMatcher
    added: int = 0

    def add_extraction(self, invoice_id: str, fields: dict[str, Any]) -> None:
        # Each new row is the invoice's latest extraction.
        self.added += 1
        insert_extraction(self.conn, invoice_id, fields, T0 + timedelta(seconds=self.added))


def load_fixture(test_db: ScratchDatabase) -> None:
    with psycopg.connect(test_db.url) as conn:
        loader.load(conn, loader.read_master(FIXTURE))


@pytest.fixture(params=["memory", "sql"])
def harness(request: pytest.FixtureRequest) -> Iterator[Harness]:
    if request.param == "memory":
        yield MemoryHarness()
        return
    test_db: ScratchDatabase = request.getfixturevalue("test_db")
    load_fixture(test_db)
    engine: Engine = request.getfixturevalue("app_engine")
    with engine.connect() as conn:
        yield SqlHarness(conn, SqlMatcher(conn))


def inv(n: int) -> str:
    return str(uuid.UUID(int=n))


def fields(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "vendor_name": "Beta Test Supplies GmbH",
        "vendor_vat_id": "DE000000002",
        "invoice_number": "F-2026-0307",
        "issue_date": "2026-02-10",
        "po_number": "PO-2026-0002",
        "subtotal": "100.00",
        "total": "119.00",
    }
    return {**base, **overrides}


# --- vendor --------------------------------------------------------------------------------------


def test_vendor_by_vat_id_ignores_spacing_and_case(harness: Harness) -> None:
    match = harness.matcher.find_vendor("de 000.000.002", None, None)
    assert match is not None
    assert (match.vendor.id, match.method, match.score) == ("V002", "vat_id", 1.0)
    assert match.vendor.country == "DE"


def test_vendor_by_iban_of_a_past_account(harness: Harness) -> None:
    match = harness.matcher.find_vendor("XX123", "IT60 X054 2811 1010 0000 0123 456", None)
    assert match is not None
    assert (match.vendor.id, match.method) == ("V001", "iban")


def test_vendor_by_name(harness: Harness) -> None:
    match = harness.matcher.find_vendor(None, None, "Beta Test Supplies")
    assert match is not None
    assert (match.vendor.id, match.method) == ("V002", "name")
    assert match.score == trigram_similarity("Beta Test Supplies", "Beta Test Supplies GmbH")
    assert match.score >= 0.6


def test_vendor_name_below_the_threshold_is_no_match(harness: Harness) -> None:
    assert harness.matcher.find_vendor(None, None, "Maillot Trading Synthetica SARL") is None
    assert harness.matcher.find_vendor(None, None, None) is None
    assert harness.matcher.find_vendor("", "", "") is None


def test_vendor_vat_id_wins_over_iban_and_name(harness: Harness) -> None:
    match = harness.matcher.find_vendor(
        "IT00000000001", "DE89370400440532013000", "Beta Test Supplies GmbH"
    )
    assert match is not None
    assert (match.vendor.id, match.method) == ("V001", "vat_id")


def test_bank_accounts_in_validity_order(harness: Harness) -> None:
    assert harness.matcher.bank_accounts("V001") == (
        BankAccountRecord("IT60X0542811101000000123456", date(2024, 1, 1), date(2025, 6, 30)),
        BankAccountRecord("IT02A0301503200000003517230", date(2025, 7, 1), None),
    )
    assert harness.matcher.bank_accounts("V999") == ()


# --- purchase order ------------------------------------------------------------------------------


def test_po_context_with_lines(harness: Harness) -> None:
    po = harness.matcher.get_po_context("PO-2026-0002")
    assert po is not None
    assert (po.vendor_id, po.currency, po.issue_date) == ("V002", "EUR", date(2026, 2, 3))
    assert po.total_net == Decimal("300.50")
    assert po.lines == (
        POLineRecord(1, "SKU-100", Decimal("2.5"), Decimal("100.20")),
        POLineRecord(2, "SKU-101", Decimal("10"), Decimal("4.99")),
    )
    assert po.billed_before == 0
    assert (po.billed_invoice_ids, po.billed_unreadable_ids) == ((), ())
    assert harness.matcher.get_po_context("PO-2026-9999") is None


def test_po_billed_so_far_by_other_invoices(harness: Harness) -> None:
    harness.add_extraction(inv(1), fields(subtotal="100.00"))
    harness.add_extraction(inv(2), fields(subtotal="not a number"))
    harness.add_extraction(inv(3), fields(po_number="PO-2026-0001", subtotal="50.00"))
    po = harness.matcher.get_po_context("PO-2026-0002")
    assert po is not None
    assert po.billed_before == Decimal("100.00")
    assert po.billed_invoice_ids == (inv(1),)
    assert po.billed_unreadable_ids == (inv(2),)
    po = harness.matcher.get_po_context("PO-2026-0002", exclude_invoice_id=inv(1))
    assert po is not None
    assert po.billed_before == 0 and po.billed_invoice_ids == ()


def test_po_billed_uses_each_invoices_latest_extraction(harness: Harness) -> None:
    harness.add_extraction(inv(1), fields(subtotal="100.00"))
    harness.add_extraction(inv(1), fields(po_number="PO-2026-0001"))
    po = harness.matcher.get_po_context("PO-2026-0002")
    assert po is not None
    assert po.billed_invoice_ids == ()


# --- contracts, VAT, tolerance -------------------------------------------------------------------


def test_contract_prices_in_force_on_the_date(harness: Harness) -> None:
    prices = (ContractPriceRecord("C001", "EUR", "SKU-001", Decimal("125.00")),)
    assert harness.matcher.contract_prices("V001", date(2026, 3, 1)) == prices
    assert harness.matcher.contract_prices("V001", date(2026, 12, 31)) == prices
    assert harness.matcher.contract_prices("V001", date(2027, 1, 1)) == ()
    assert harness.matcher.contract_prices("V002", date(2026, 3, 1)) == ()


def test_vat_rates_and_tolerance(harness: Harness) -> None:
    assert harness.matcher.vat_rates() == {"IT": Decimal("22.00"), "DE": Decimal("19.00")}
    assert harness.matcher.po_tolerance_pct() == Decimal("2.00")


# --- duplicates ----------------------------------------------------------------------------------


def _dupes(
    harness: Harness, norm: str, total: str | None = "119.00", day: str | None = "2026-02-10"
) -> list[tuple[str, str]]:
    found = harness.matcher.find_duplicates(
        "V002",
        norm,
        Decimal(total) if total else None,
        date.fromisoformat(day) if day else None,
        exclude_invoice_id=inv(99),
    )
    return [(c.invoice_id, c.match) for c in found]


def test_exact_duplicate_on_the_normalised_number(harness: Harness) -> None:
    harness.add_extraction(inv(1), fields())
    record = extraction_record(fields(invoice_number="F 2026 307"))
    assert record.invoice_number_norm is not None
    # Exact matches need neither the amount nor the date.
    assert _dupes(harness, record.invoice_number_norm, total=None, day=None) == [(inv(1), "exact")]
    candidate = harness.matcher.find_duplicates("V002", "F2026307", None, None)[0]
    assert candidate.invoice_number == "F-2026-0307"
    assert (candidate.total, candidate.issue_date) == (Decimal("119.00"), date(2026, 2, 10))
    assert candidate.similarity == 1.0


def test_fuzzy_duplicate_needs_amount_date_and_a_similar_number(harness: Harness) -> None:
    harness.add_extraction(inv(1), fields())
    assert trigram_similarity("F2026308", "F2026307") >= 0.6
    assert _dupes(harness, "F2026308", total="119.01", day="2026-02-20") == [(inv(1), "fuzzy")]
    assert _dupes(harness, "F2026308", total="119.02") == []
    assert _dupes(harness, "F2026308", day="2026-02-21") == []
    assert _dupes(harness, "F2026308", total=None) == []
    assert _dupes(harness, "X9999") == []


def test_duplicates_only_for_this_vendor_and_not_itself(harness: Harness) -> None:
    harness.add_extraction(inv(1), fields(vendor_vat_id="IT00000000001"))
    harness.add_extraction(inv(99), fields())
    assert _dupes(harness, "F2026307") == []


def test_duplicates_use_each_invoices_latest_extraction(harness: Harness) -> None:
    harness.add_extraction(inv(1), fields())
    harness.add_extraction(inv(1), fields(invoice_number="Z-1", total="5.00"))
    assert _dupes(harness, "F2026307") == []
