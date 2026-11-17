"""Normalisation helpers, including the copy of synth's invoice-number key."""

from decimal import Decimal

import pytest

from app.extraction.normalize import compact, normalize_invoice_number, normalize_text, to_decimal


@pytest.mark.parametrize(
    ("number", "key"),
    [
        ("RE-00356", "RE356"),
        ("RE 356", "RE356"),
        ("re/356", "RE356"),
        ("2026/326", "2026326"),
        ("FAC-2026-0293", "FAC2026293"),
        ("INV-0141", "INV141"),
        ("---", ""),
    ],
)
def test_invoice_number_key_matches_synth(number: str, key: str) -> None:
    # A number and the variants synth's reformat_invoice_number makes of it share one key.
    assert normalize_invoice_number(number) == key


def test_text_and_compact_forms() -> None:
    assert normalize_text("  Paper CUPS\n200 ml ") == "paper cups 200 ml"
    assert compact("es25 zzzz 7221") == "ES25ZZZZ7221"


def test_to_decimal() -> None:
    assert to_decimal("12.40") == Decimal("12.4")
    assert to_decimal(None) is None
    assert to_decimal("twelve") is None
