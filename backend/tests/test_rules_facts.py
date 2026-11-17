"""parse_facts: extracted fields as typed values; anything missing or unparsable becomes None."""

import json
from datetime import date
from decimal import Decimal
from typing import Any

from app.rules.facts import parse_facts, to_decimal
from tests.support import SEED_INVOICES


def _seed_fields(name: str = "INV-SEED-001") -> dict[str, Any]:
    fields: dict[str, Any] = json.loads((SEED_INVOICES / f"{name}.json").read_text())["fields"]
    return fields


def test_seed_ground_truth_parses_cleanly() -> None:
    facts = parse_facts(_seed_fields())
    assert facts.parse_errors == ()
    assert facts.vendor_vat_id == "ES997039099"
    assert facts.invoice_number == "RE-00339"
    assert facts.issue_date == date(2026, 1, 23)
    assert facts.total == Decimal("1736.01")
    assert facts.vat_rate == Decimal("21.00")
    assert facts.lines is not None and len(facts.lines) == 4
    first = facts.lines[0]
    assert (first.sku, first.quantity, first.unit_price, first.line_total) == (
        "SKU-034",
        Decimal("50"),
        Decimal("6.77"),
        Decimal("338.50"),
    )


def test_missing_and_blank_fields_are_none_without_a_parse_error() -> None:
    fields = _seed_fields()
    del fields["iban"]
    fields["po_number"] = "   "
    fields["total"] = None
    facts = parse_facts(fields)
    assert (facts.iban, facts.po_number, facts.total) == (None, None, None)
    assert facts.parse_errors == ()


def test_unparsable_values_become_none_with_a_parse_error() -> None:
    fields = _seed_fields()
    fields["total"] = "about 1700"
    fields["issue_date"] = "23/01/2026"
    fields["vendor_name"] = 42
    fields["subtotal"] = True
    fields["lines"][1]["quantity"] = "sixteen"
    facts = parse_facts(fields)
    assert facts.total is None and facts.issue_date is None and facts.vendor_name is None
    assert facts.subtotal is None
    assert facts.lines is not None and facts.lines[1].quantity is None
    assert {e.field for e in facts.parse_errors} == {
        "total",
        "issue_date",
        "vendor_name",
        "subtotal",
        "lines[1].quantity",
    }


def test_numbers_accept_ints_and_floats_but_not_nan() -> None:
    fields = _seed_fields()
    fields["subtotal"] = 1434.72
    fields["vat_rate"] = 21
    fields["total"] = "NaN"
    facts = parse_facts(fields)
    assert facts.subtotal == Decimal("1434.72")
    assert facts.vat_rate == Decimal("21")
    assert facts.total is None
    assert [e.field for e in facts.parse_errors] == ["total"]


def test_lines_that_are_not_a_list_are_a_parse_error() -> None:
    fields = _seed_fields()
    fields["lines"] = "four lines"
    facts = parse_facts(fields)
    assert facts.lines is None
    assert [e.field for e in facts.parse_errors] == ["lines"]


def test_a_line_that_is_not_an_object_is_a_parse_error() -> None:
    fields = _seed_fields()
    fields["lines"][2] = "Tea bags"
    facts = parse_facts(fields)
    assert facts.lines is not None and len(facts.lines) == 4
    assert facts.lines[2].sku is None
    assert [e.field for e in facts.parse_errors] == ["lines[2]"]


def test_non_mapping_input_is_one_parse_error() -> None:
    facts = parse_facts("not a dict")  # type: ignore[arg-type]
    assert facts.invoice_number is None and facts.lines is None
    assert [e.field for e in facts.parse_errors] == ["fields"]


def test_to_decimal_reads_like_parse_facts() -> None:
    assert to_decimal("2.00") == Decimal("2.00")
    assert to_decimal(3) == Decimal(3)
    assert to_decimal(" ") is None
    assert to_decimal("two") is None
    assert to_decimal(True) is None
