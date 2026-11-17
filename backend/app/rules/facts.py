"""InvoiceFacts: the extracted invoice fields as typed values, for the rules to check.

`parse_facts` never raises. A value that is absent or blank becomes None; a value that is
present but cannot be read (wrong type, bad number or date) also becomes None and adds a
`ParseError`, which the rules turn into a flag (fail closed).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class ParseError:
    field: str
    message: str


@dataclass(frozen=True)
class LineFacts:
    line_no: int | None = None
    sku: str | None = None
    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    line_total: Decimal | None = None


@dataclass(frozen=True)
class InvoiceFacts:
    vendor_name: str | None = None
    vendor_vat_id: str | None = None
    vendor_country: str | None = None
    invoice_number: str | None = None
    issue_date: date | None = None
    due_date: date | None = None
    currency: str | None = None
    po_number: str | None = None
    iban: str | None = None
    # None when the field is missing or not a list; an empty tuple when the list is empty.
    lines: tuple[LineFacts, ...] | None = None
    subtotal: Decimal | None = None
    vat_rate: Decimal | None = None
    vat_amount: Decimal | None = None
    total: Decimal | None = None
    parse_errors: tuple[ParseError, ...] = ()

    def parse_error(self, field: str) -> ParseError | None:
        return next((e for e in self.parse_errors if e.field == field), None)


class _Reader:
    def __init__(self) -> None:
        self.errors: list[ParseError] = []

    def _absent(self, value: object) -> bool:
        return value is None or (isinstance(value, str) and not value.strip())

    def fail(self, field: str, message: str) -> None:
        self.errors.append(ParseError(field, message))

    def text(self, raw: Mapping[str, Any], key: str, prefix: str = "") -> str | None:
        value = raw.get(key)
        if self._absent(value):
            return None
        if not isinstance(value, str):
            self.fail(prefix + key, f"expected text, got {type(value).__name__}")
            return None
        return value.strip()

    def decimal(self, raw: Mapping[str, Any], key: str, prefix: str = "") -> Decimal | None:
        value = raw.get(key)
        if self._absent(value):
            return None
        if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
            self.fail(prefix + key, f"expected a number, got {type(value).__name__}")
            return None
        try:
            number = Decimal(value.strip() if isinstance(value, str) else str(value))
        except InvalidOperation:
            self.fail(prefix + key, f"not a number: {str(value)[:40]!r}")
            return None
        if not number.is_finite():
            self.fail(prefix + key, f"not a finite number: {str(value)[:40]!r}")
            return None
        return number

    def day(self, raw: Mapping[str, Any], key: str) -> date | None:
        value = raw.get(key)
        if self._absent(value):
            return None
        if not isinstance(value, str):
            self.fail(key, f"expected an ISO date, got {type(value).__name__}")
            return None
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            self.fail(key, f"not an ISO date: {value[:40]!r}")
            return None

    def integer(self, raw: Mapping[str, Any], key: str, prefix: str) -> int | None:
        value = raw.get(key)
        if self._absent(value):
            return None
        if isinstance(value, bool) or not isinstance(value, int | str):
            self.fail(prefix + key, f"expected an integer, got {type(value).__name__}")
            return None
        try:
            return int(value)
        except ValueError:
            self.fail(prefix + key, f"not an integer: {str(value)[:40]!r}")
            return None

    def line(self, raw: object, index: int) -> LineFacts:
        prefix = f"lines[{index}]"
        if not isinstance(raw, Mapping):
            self.fail(prefix, f"expected an object, got {type(raw).__name__}")
            return LineFacts()
        prefix += "."
        return LineFacts(
            line_no=self.integer(raw, "line_no", prefix),
            sku=self.text(raw, "sku", prefix),
            description=self.text(raw, "description", prefix),
            quantity=self.decimal(raw, "quantity", prefix),
            unit_price=self.decimal(raw, "unit_price", prefix),
            line_total=self.decimal(raw, "line_total", prefix),
        )

    def lines(self, raw: Mapping[str, Any]) -> tuple[LineFacts, ...] | None:
        value = raw.get("lines")
        if value is None:
            return None
        if isinstance(value, str | bytes) or not isinstance(value, Sequence):
            self.fail("lines", f"expected a list, got {type(value).__name__}")
            return None
        return tuple(self.line(item, i) for i, item in enumerate(value))


def to_decimal(value: object) -> Decimal | None:
    """A number read the way `parse_facts` reads one; None if absent or unreadable."""
    return _Reader().decimal({"value": value}, "value")


def parse_facts(fields: Mapping[str, Any]) -> InvoiceFacts:
    """Typed facts from extracted fields shaped like the seed ground truth's `fields`."""
    reader = _Reader()
    if not isinstance(fields, Mapping):
        reader.fail("fields", f"expected an object, got {type(fields).__name__}")
        return InvoiceFacts(parse_errors=tuple(reader.errors))
    facts = InvoiceFacts(
        vendor_name=reader.text(fields, "vendor_name"),
        vendor_vat_id=reader.text(fields, "vendor_vat_id"),
        vendor_country=reader.text(fields, "vendor_country"),
        invoice_number=reader.text(fields, "invoice_number"),
        issue_date=reader.day(fields, "issue_date"),
        due_date=reader.day(fields, "due_date"),
        currency=reader.text(fields, "currency"),
        po_number=reader.text(fields, "po_number"),
        iban=reader.text(fields, "iban"),
        lines=reader.lines(fields),
        subtotal=reader.decimal(fields, "subtotal"),
        vat_rate=reader.decimal(fields, "vat_rate"),
        vat_amount=reader.decimal(fields, "vat_amount"),
        total=reader.decimal(fields, "total"),
    )
    return replace(facts, parse_errors=tuple(reader.errors))
