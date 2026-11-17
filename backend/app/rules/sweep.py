"""Regex sweep for IBANs, invoice numbers and totals over all PDF text and metadata.

The rules check every value found here, hidden text included, not only the extracted ones
(docs/decisions.md, "Rules"). Totals are taken only where a label says so ("Total",
"Amount due", ...): invoice lines and the subtotal carry amounts too, and must not count.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from app.rules.iban import find_ibans
from app.rules.numbers import parse_amount

_FLAGS = re.IGNORECASE | re.MULTILINE
_SP = r"[^\S\n]"  # whitespace on the same line

# A candidate invoice number: tokens of letters, digits and / . _ - separated by single spaces.
_VALUE = rf"(?P<v>[A-Za-z0-9](?:[A-Za-z0-9/._-]|{_SP}(?=[A-Za-z0-9]))*)"
_NUMBER_PATTERNS = (
    # "Invoice number: RE-00339", "Invoice no. 42", "Invoice #42"
    re.compile(
        rf"\binvoice{_SP}+(?:number|no\.?|nr\.?|num\.?|#){_SP}*[:.#]?{_SP}*{_VALUE}", _FLAGS
    ),
    # "Please quote RE-00339 with your payment"
    re.compile(
        rf"\bplease{_SP}+quote{_SP}+(?:(?:the{_SP}+)?invoice{_SP}+(?:number{_SP}+)?)?{_VALUE}",
        _FLAGS,
    ),
    # A heading or PDF title: "Invoice RE-00339", "Invoice RE-00339 from <vendor>"
    re.compile(rf"^{_SP}*invoice{_SP}+(?!(?:number|no|nr|num)\b){_VALUE}", _FLAGS),
)
_MAX_NUMBER_TOKENS = 6

_TOTAL = re.compile(
    r"(?<![\w-])(?<!line )"
    r"(?:grand\s+total|total\s+(?:due|payable)|total\s+amount(?:\s+due)?|amount\s+due"
    r"|balance\s+due|total)"
    rf"(?!{_SP}+(?:vat|tax|net|excl))\b"
    rf"{_SP}*(?:\({_SP}*[A-Z]{{3}}{_SP}*\)|[A-Z]{{3}}|[€$£])?{_SP}*:?{_SP}*(?:[A-Z]{{3}}|[€$£])?{_SP}*"
    r"(?P<v>\d[\d.,']*\d|\d)",
    _FLAGS,
)


@dataclass(frozen=True)
class FoundValue:
    value: str
    source: str  # "text" or "metadata:<key>"


@dataclass(frozen=True)
class FoundAmount:
    value: Decimal
    raw: str
    source: str


@dataclass(frozen=True)
class TextSweep:
    ibans: tuple[FoundValue, ...] = ()
    invoice_numbers: tuple[FoundValue, ...] = ()
    totals: tuple[FoundAmount, ...] = ()


def _trim_number(captured: str) -> str | None:
    """Keep the tokens that make up the number: "RE 356" stays whole, "RE-00339 with" stops.

    Tokens are taken while no digit has been seen yet ("INV" then "42"), then only tokens
    that look like more of the number (a digit, no lowercase letter).
    """
    kept: list[str] = []
    for token in captured.split()[:_MAX_NUMBER_TOKENS]:
        has_digit = any(ch.isdigit() for ch in token)
        if kept and any(ch.isdigit() for ch in "".join(kept)):
            if not has_digit or any(ch.islower() for ch in token):
                break
        kept.append(token)
    value = " ".join(kept).rstrip("./_-")
    return value if any(ch.isdigit() for ch in value) else None


def _numbers(text: str, source: str) -> list[tuple[int, FoundValue]]:
    found: dict[int, FoundValue] = {}
    for pattern in _NUMBER_PATTERNS:
        for match in pattern.finditer(text):
            value = _trim_number(match.group("v"))
            # Two labels can point at the same value ("Please quote invoice number X").
            if value is not None and match.start("v") not in found:
                found[match.start("v")] = FoundValue(value, source)
    return sorted(found.items())


def _totals(text: str, source: str) -> list[FoundAmount]:
    found = []
    for match in _TOTAL.finditer(text):
        amount = parse_amount(match.group("v"))
        if amount is not None:
            found.append(FoundAmount(amount, match.group("v"), source))
    return found


def sweep_text(text: str, metadata: Mapping[str, str]) -> TextSweep:
    """Every IBAN, invoice number and labelled total in the page text, then each metadata value."""
    sources = [("text", text), *((f"metadata:{key}", metadata[key]) for key in sorted(metadata))]
    ibans: list[FoundValue] = []
    numbers: list[FoundValue] = []
    totals: list[FoundAmount] = []
    for source, body in sources:
        ibans += [FoundValue(iban, source) for iban in find_ibans(body)]
        numbers += [found for _, found in _numbers(body, source)]
        totals += _totals(body, source)
    return TextSweep(ibans=tuple(ibans), invoice_numbers=tuple(numbers), totals=tuple(totals))
