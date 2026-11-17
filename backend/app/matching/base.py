"""The matching Protocol shared by the SQL matcher (the app) and the in-memory one (Layer-1 eval).

Both answer the same lookups over the same master data and earlier extractions, with the
same thresholds, so the eval measures exactly what the app does (a parity test holds them
together on the seed).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from app.rules.context import (
    BankAccountRecord,
    ContractPriceRecord,
    DuplicateCandidate,
    PurchaseOrderContext,
    VendorMatch,
)
from app.rules.facts import parse_facts
from app.rules.numbers import CENT, normalize_invoice_number

# A name match counts only from here up (pg_trgm similarity). On the seed the closest two
# real vendors score 0.44 and the invented vendor at most 0.31.
VENDOR_NAME_THRESHOLD = 0.6
# Fuzzy duplicate: same vendor, amount within a cent, issue dates at most 10 days apart, and
# the normalised numbers at least this similar.
DUPLICATE_NUMBER_THRESHOLD = 0.6
DUPLICATE_DATE_WINDOW_DAYS = 10
DUPLICATE_AMOUNT_TOLERANCE = CENT
CLAUSE_SEARCH_LIMIT = 5

_NOT_ALNUM = re.compile(r"[^A-Za-z0-9]")


def compact_id(value: str) -> str:
    """VAT ids and IBANs compare without spaces, dots or dashes, in upper case."""
    return _NOT_ALNUM.sub("", value).upper()


@dataclass(frozen=True)
class ExtractionRecord:
    """The searchable columns of an `extractions` row, derived from its `fields`.

    Whoever writes an extraction row stores these values, so the duplicate search compares
    like with like: the VAT id compacted, the number normalised.
    """

    vendor_vat_id: str | None
    invoice_number: str | None
    invoice_number_norm: str | None
    total: Decimal | None
    issue_date: date | None


def extraction_record(fields: Mapping[str, Any]) -> ExtractionRecord:
    facts = parse_facts(fields)
    norm = normalize_invoice_number(facts.invoice_number) if facts.invoice_number else None
    return ExtractionRecord(
        vendor_vat_id=compact_id(facts.vendor_vat_id) if facts.vendor_vat_id else None,
        invoice_number=facts.invoice_number,
        invoice_number_norm=norm or None,
        total=facts.total,
        issue_date=facts.issue_date,
    )


class Matcher(Protocol):
    def find_vendor(
        self, vat_id: str | None, iban: str | None, name: str | None
    ) -> VendorMatch | None:
        """Exact VAT id, then exact IBAN on any of a vendor's accounts, then a name match."""
        ...

    def bank_accounts(self, vendor_id: str) -> tuple[BankAccountRecord, ...]: ...

    def get_po_context(
        self, po_id: str, exclude_invoice_id: str | None = None
    ) -> PurchaseOrderContext | None:
        """The PO with its lines and what the other invoices' latest extractions billed on it."""
        ...

    def contract_prices(self, vendor_id: str, on_date: date) -> tuple[ContractPriceRecord, ...]:
        """Prices of the vendor's contracts in force on `on_date`."""
        ...

    def find_duplicates(
        self,
        vendor_id: str,
        invoice_number_norm: str,
        total: Decimal | None,
        issue_date: date | None,
        exclude_invoice_id: str | None = None,
    ) -> tuple[DuplicateCandidate, ...]:
        """Other invoices of the vendor: same normalised number, or a fuzzy near match."""
        ...

    def vat_rates(self) -> dict[str, Decimal]: ...

    def po_tolerance_pct(self) -> Decimal | None:
        """None when the setting is missing or unreadable (the rules then fail closed)."""
        ...
