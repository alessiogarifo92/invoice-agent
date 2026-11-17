"""Normalisation shared by the grounding check and the Layer-2 eval.

The invoice-number key lives in app.rules.numbers only: the rules, the duplicate search and
the extraction writer all use that one function.
"""

import re
import unicodedata
from decimal import Decimal, InvalidOperation

_WHITESPACE = re.compile(r"\s+")


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
