"""The grounding check: every value the model extracted must be printed in the document.

A value that is not in the text was invented (or misread), so it raises a
`grounding_failure` flag rather than reaching the rules unchallenged. Matching normalises:
- text (names, address, description, codes): NFKC, case and whitespace, on word boundaries;
- ids (VAT id, invoice and PO number, IBAN, SKU): case, plus any spacing between characters
  ("ES25 ZZZZ 7221..."), still on word boundaries so a truncated id is not grounded;
- numbers: "1250.00", "1,250.00", "1.250,00", "1 250,00" are one value; a single separator
  followed by three digits ("1,250") reads both ways. A number must be a whole token: the
  "25" inside "ES25ZZZZ..." does not ground 25. The sign is part of the number;
- dates: ISO, YYYY/MM/DD, and the common EU forms DD/MM/YYYY, DD.MM.YYYY, DD-MM-YYYY and
  "23 January 2026" / "23 Jan 2026". Numeric dates are read day first, never month first.

Callers pass `visible_text`, never `all_text`: a value present only in hidden text (white
on white, a tiny font) is then NOT grounded, so an extraction that picked up an attacker's
hidden IBAN is flagged. The rules sweep every IBAN in `all_text` separately (phase 3).
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

from app.extraction.normalize import compact, normalize_text
from app.extraction.schema import InvoiceExtraction, LineExtraction

VALUE_MAX = 200

Kind = Literal["text", "id", "number", "date"]

FIELD_KINDS: dict[str, Kind] = {
    "vendor_name": "text",
    "vendor_vat_id": "id",
    "vendor_country": "text",
    "vendor_address": "text",
    "invoice_number": "id",
    "issue_date": "date",
    "due_date": "date",
    "currency": "text",
    "po_number": "id",
    "iban": "id",
    "subtotal": "number",
    "vat_rate": "number",
    "vat_amount": "number",
    "total": "number",
}
LINE_KINDS: dict[str, Kind] = {
    "line_no": "number",
    "sku": "id",
    "description": "text",
    "quantity": "number",
    "unit_price": "number",
    "line_total": "number",
}

# A number token: not glued to a word or to another number. A "-" glued to a word
# ("PO-2026", "2026-01") is a separator, not a sign.
_NUMBER = re.compile(r"(?<![\w.,])-?\d+(?:[.,]\d+)*(?!\w)")
# The same, with a space (NBSP and narrow NBSP become spaces under NFKC) between thousands.
_SPACED_NUMBER = re.compile(r"(?<![\w.,])-?\d{1,3}(?: \d{3})+(?:[.,]\d{1,2})?(?![\w.,])")

_ISO_DATE = re.compile(r"(?<!\d)(\d{4})([-/])(\d{2})\2(\d{2})(?!\d)")
_EU_DATE = re.compile(r"(?<!\d)(\d{1,2})([./-])(\d{1,2})\2(\d{4})(?!\d)")
_MONTHS = {
    name: number
    for number, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for name in names
}
_NAMED_DATE = re.compile(
    r"(?<!\d)(\d{1,2})\.?\s+(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?,?"
    r"\s+(\d{4})(?!\d)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GroundingIssue:
    """An extracted value not found in the document text. `field` is a path: lines[0].sku."""

    field: str
    value: str


def number_readings(token: str) -> set[Decimal]:
    """Every value a printed number can mean, given the usual separator conventions."""
    sign = "-" if token.startswith("-") else ""
    digits = token.lstrip("-").replace(" ", "")
    dots, commas = digits.count("."), digits.count(",")
    candidates: list[str]
    if dots and commas:
        decimal_sep = "." if digits.rfind(".") > digits.rfind(",") else ","
        thousands_sep = "," if decimal_sep == "." else "."
        if digits.count(decimal_sep) > 1:
            return set()
        candidates = [digits.replace(thousands_sep, "").replace(decimal_sep, ".")]
    elif dots or commas:
        sep = "." if dots else ","
        if dots + commas > 1:
            candidates = [digits.replace(sep, "")]
        else:
            whole, fraction = digits.split(sep)
            candidates = [f"{whole}.{fraction}"]
            if len(fraction) == 3:
                candidates.append(whole + fraction)
    else:
        candidates = [digits]
    readings = set()
    for candidate in candidates:
        try:
            readings.add(Decimal(sign + candidate))
        except InvalidOperation:
            continue
    return readings


def date_readings(text: str) -> set[date]:
    """Every date printed in `text`, in the formats the module docstring lists."""
    found: set[date] = set()
    parts: list[tuple[str, str, str]] = []
    parts += [(y, m, d) for y, _, m, d in _ISO_DATE.findall(text)]
    parts += [(y, m, d) for d, _, m, y in _EU_DATE.findall(text)]
    parts += [(y, str(_MONTHS[m.lower()]), d) for d, m, y in _NAMED_DATE.findall(text)]
    for year, month, day in parts:
        try:
            found.add(date(int(year), int(month), int(day)))
        except ValueError:
            continue
    return found


class _Document:
    """The text, indexed once for every lookup."""

    def __init__(self, text: str):
        nfkc = unicodedata.normalize("NFKC", text)
        self.text = normalize_text(text)
        self.numbers: set[Decimal] = set()
        for pattern in (_NUMBER, _SPACED_NUMBER):
            for match in pattern.finditer(nfkc):
                self.numbers |= number_readings(match.group())
        self.dates = date_readings(nfkc)

    def has_text(self, value: str) -> bool:
        needle = normalize_text(value)
        if not needle:
            return False
        return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", self.text) is not None

    def has_id(self, value: str) -> bool:
        chars = compact(value).casefold()
        if not chars:
            return False
        spaced = r"\s*".join(re.escape(c) for c in chars)
        return re.search(rf"(?<!\w){spaced}(?!\w)", self.text) is not None

    def has_number(self, value: str) -> bool:
        try:
            return Decimal(value) in self.numbers
        except InvalidOperation:
            return False

    def has_date(self, value: str) -> bool:
        try:
            return date.fromisoformat(value) in self.dates
        except ValueError:
            return False

    def has(self, kind: Kind, value: str) -> bool:
        if kind == "text":
            return self.has_text(value)
        if kind == "id":
            return self.has_id(value)
        if kind == "number":
            return self.has_number(value)
        return self.has_date(value)


def _check(
    doc: _Document,
    model: InvoiceExtraction | LineExtraction,
    kinds: dict[str, Kind],
    prefix: str,
) -> list[GroundingIssue]:
    issues = []
    for name, kind in kinds.items():
        raw = getattr(model, name)
        if raw is None:
            continue
        value = str(raw)
        if not doc.has(kind, value):
            issues.append(GroundingIssue(field=prefix + name, value=value[:VALUE_MAX]))
    return issues


def check_grounding(extraction: InvoiceExtraction, text: str) -> list[GroundingIssue]:
    """The extracted values (nulls skipped) that `text` does not contain, in schema order."""
    doc = _Document(text)
    issues = _check(doc, extraction, FIELD_KINDS, "")
    for index, line in enumerate(extraction.lines):
        issues += _check(doc, line, LINE_KINDS, f"lines[{index}].")
    return issues
