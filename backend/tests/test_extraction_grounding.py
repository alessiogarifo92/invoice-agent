"""The grounding check: every extracted value must appear in the visible document text."""

import json
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.extraction.grounding import GroundingIssue, check_grounding, date_readings, number_readings
from app.extraction.schema import InvoiceExtraction
from app.ingestion.hidden import all_text, visible_text
from app.ingestion.pdf import extract_document
from tests.support import SEED_INVOICES

TEXT = """\
Posada Catering Synthetica S.L.
Cañada Américo Marí 50, 34048 Córdoba, Spain
VAT ID: ES997039099
Country: ES (Spain)
Invoice number: RE-00339
Issue date: 2026-01-23
Due date: 2026-02-22
PO number: PO-2026-0001
Currency: EUR
# SKU Description Qty Unit price Line total
1 SKU-034 Paper cups 200 ml, pack of 100 50 6.77 338.50
Subtotal 338.50
VAT rate 21.00% 71.09
Total EUR 409.59
IBAN: ES25ZZZZ7221269179025458
"""

LINE = {
    "line_no": 1,
    "sku": "SKU-034",
    "description": "Paper cups 200 ml, pack of 100",
    "quantity": "50",
    "unit_price": "6.77",
    "line_total": "338.50",
}
FIELDS: dict[str, Any] = {
    "vendor_name": "Posada Catering Synthetica S.L.",
    "vendor_vat_id": "ES997039099",
    "vendor_country": "ES",
    "vendor_address": "Cañada Américo Marí 50, 34048 Córdoba, Spain",
    "invoice_number": "RE-00339",
    "issue_date": "2026-01-23",
    "due_date": "2026-02-22",
    "currency": "EUR",
    "po_number": "PO-2026-0001",
    "iban": "ES25ZZZZ7221269179025458",
    "lines": [LINE],
    "subtotal": "338.50",
    "vat_rate": "21.00",
    "vat_amount": "71.09",
    "total": "409.59",
}


def _extraction(**overrides: Any) -> InvoiceExtraction:
    return InvoiceExtraction.model_validate({**FIELDS, **overrides})


def _fields(issues: list[GroundingIssue]) -> list[str]:
    return [issue.field for issue in issues]


def test_a_faithful_extraction_is_grounded() -> None:
    assert check_grounding(_extraction(), TEXT) == []


def test_null_values_need_no_grounding() -> None:
    nulls = {name: None for name in FIELDS if name != "lines"}
    line = {name: None for name in LINE}
    assert check_grounding(_extraction(**nulls, lines=[line]), "") == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("total", "409.95"),
        ("iban", "ES25ZZZZ7221269179025459"),
        ("invoice_number", "RE-00393"),
        ("vendor_name", "Posada Catering S.L."),
        ("issue_date", "2026-01-24"),
        ("currency", "USD"),
        ("vendor_country", "FR"),
        ("vendor_vat_id", "ES997039090"),
        ("po_number", "PO-2026-0002"),
    ],
)
def test_a_hallucinated_scalar_is_reported(field: str, value: str) -> None:
    issues = check_grounding(_extraction(**{field: value}), TEXT)
    assert issues == [GroundingIssue(field=field, value=value)]


def test_hallucinated_line_values_are_reported_with_their_path() -> None:
    lines = [LINE, {**LINE, "line_no": 7, "sku": "SKU-999", "unit_price": "7.77"}]
    issues = check_grounding(_extraction(lines=lines), TEXT)
    assert _fields(issues) == ["lines[1].line_no", "lines[1].sku", "lines[1].unit_price"]
    assert issues[1].value == "SKU-999"


@pytest.mark.parametrize(
    "printed", ["1250.00", "1,250.00", "1.250,00", "1 250,00", "1 250,00", "EUR 1250"]
)
def test_reformatted_numbers_are_grounded(printed: str) -> None:
    text = f"Total {printed}\n"
    assert check_grounding(_extraction(total="1250.00"), TEXT + text) == []
    assert check_grounding(_extraction(total="1250"), TEXT + text) == []


def test_number_readings_cover_ambiguous_separators() -> None:
    assert number_readings("1,250") == {Decimal("1250"), Decimal("1.250")}
    assert number_readings("1.234.567") == {Decimal("1234567")}
    assert number_readings("1.250,00") == {Decimal("1250.00")}
    assert number_readings("21.00") == {Decimal("21.00")}


def test_a_number_must_be_a_whole_token() -> None:
    # 25 is only inside the IBAN (ES25...), 7221 only inside a longer digit run, and 2026 is
    # a bare token of the dates and the PO number.
    assert _fields(check_grounding(_extraction(total="25.00"), TEXT)) == ["total"]
    assert _fields(check_grounding(_extraction(total="7221"), TEXT)) == ["total"]
    assert check_grounding(_extraction(total="2026"), TEXT) == []


def test_sign_is_part_of_the_number() -> None:
    assert _fields(check_grounding(_extraction(total="-409.59"), TEXT)) == ["total"]
    assert check_grounding(_extraction(total="-12.00"), TEXT + "Credit -12.00\n") == []


def test_ids_ignore_spacing_and_case() -> None:
    text = TEXT + "IBAN: es25 zzzz 7221 2691 7902 5458\nVAT: es 997 039 099\n"
    assert check_grounding(_extraction(iban="ES25 ZZZZ 7221 2691 7902 5458"), TEXT) == []
    assert check_grounding(_extraction(), text.replace("ES25ZZZZ", "XX")) == []


def test_text_ignores_whitespace_case_and_line_breaks() -> None:
    text = TEXT.replace("Paper cups 200 ml, pack of 100", "PAPER  cups 200 ml,\npack of 100")
    assert check_grounding(_extraction(), text) == []


def test_text_must_match_on_word_boundaries() -> None:
    # "ES" appears as a word ("Country: ES"), "Synth" only inside "Synthetica".
    assert check_grounding(_extraction(vendor_country="ES"), TEXT) == []
    assert _fields(check_grounding(_extraction(vendor_name="Synth"), TEXT)) == ["vendor_name"]


@pytest.mark.parametrize(
    "printed", ["23/01/2026", "23.01.2026", "23-01-2026", "23 January 2026", "23 Jan 2026"]
)
def test_eu_date_formats_are_grounded(printed: str) -> None:
    text = TEXT.replace("2026-01-23", printed)
    assert check_grounding(_extraction(), text) == []


def test_day_first_is_assumed_for_numeric_dates() -> None:
    assert date_readings("01/02/2026") == {date(2026, 2, 1)}
    assert date_readings("31/12/2026 2026-11-17") == {date(2026, 12, 31), date(2026, 11, 17)}
    assert date_readings("32/01/2026") == set()


def test_a_value_present_only_in_hidden_text_is_not_grounded() -> None:
    # INV-SEED-058 carries the attacker IBAN in white-on-white text: it is in all_text but
    # not in visible_text, so an extraction that picked it up is flagged.
    meta = json.loads((SEED_INVOICES / "INV-SEED-058.json").read_text())
    doc = extract_document((SEED_INVOICES / "INV-SEED-058.pdf").read_bytes())
    attacker = "ES80XZXZ2068046027930934"
    assert attacker in all_text(doc) and attacker not in visible_text(doc)
    extraction = InvoiceExtraction.model_validate({**meta["fields"], "iban": attacker})
    assert check_grounding(extraction, visible_text(doc)) == [GroundingIssue("iban", attacker)]
    assert check_grounding(extraction, all_text(doc)) == []


def test_every_seed_ground_truth_is_grounded_in_its_visible_text() -> None:
    files = sorted(SEED_INVOICES.glob("*.json"))
    for path in files:
        fields = json.loads(path.read_text())["fields"]
        doc = extract_document(path.with_suffix(".pdf").read_bytes())
        extraction = InvoiceExtraction.model_validate(fields)
        assert check_grounding(extraction, visible_text(doc)) == [], path.name


def test_issue_values_are_bounded() -> None:
    issue = check_grounding(_extraction(vendor_address="x" * 200), TEXT)[0]
    assert issue.field == "vendor_address" and len(issue.value) <= 200
