"""Normalisation shared by the grounding check, persistence and the Layer-2 eval."""

import re
import unicodedata
from decimal import Decimal, InvalidOperation

_WHITESPACE = re.compile(r"\s+")


def normalize_invoice_number(number: str) -> str:
    """Comparison key: uppercase alphanumeric groups, numeric groups without leading zeros.

    A copy of synth.invoices.normalize_invoice_number (the backend does not import synth),
    so "RE-00356" and "RE 356" share the key the duplicate search indexes.
    """
    groups = re.findall(r"[A-Za-z]+|\d+", number)
    return "".join(str(int(g)) if g.isdigit() else g.upper() for g in groups)


def normalize_text(value: str) -> str:
    """NFKC, case-folded, every whitespace run (line breaks, NBSP) as one space."""
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value).casefold()).strip()


def compact(value: str) -> str:
    """NFKC, uppercase, no whitespace at all: how IBANs and ids compare."""
    return _WHITESPACE.sub("", unicodedata.normalize("NFKC", value)).upper()


def to_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None
