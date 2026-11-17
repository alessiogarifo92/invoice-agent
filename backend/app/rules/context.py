"""RuleContext: everything the rules know besides the invoice itself.

The matching layer builds it (from SQL in the app, from an in-memory index in the Layer-1
eval); the rules only read it. Nothing here touches a database.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal, Protocol

from app.rules.sweep import TextSweep

VendorMatchMethod = Literal["vat_id", "iban", "name"]


@dataclass(frozen=True)
class VendorRecord:
    id: str
    name: str
    vat_id: str
    country: str


@dataclass(frozen=True)
class VendorMatch:
    vendor: VendorRecord
    method: VendorMatchMethod
    score: float  # 1.0 for an exact VAT id or IBAN match, the trigram similarity for a name


@dataclass(frozen=True)
class BankAccountRecord:
    iban: str
    valid_from: date
    valid_to: date | None  # None: the vendor's current account


@dataclass(frozen=True)
class POLineRecord:
    line_no: int
    sku: str
    quantity: Decimal
    unit_price: Decimal


@dataclass(frozen=True)
class PurchaseOrderContext:
    id: str
    vendor_id: str
    currency: str
    issue_date: date
    total_net: Decimal
    lines: tuple[POLineRecord, ...]
    # Net amount already billed against this PO by other invoices (their latest extraction).
    billed_before: Decimal = Decimal(0)
    billed_invoice_ids: tuple[str, ...] = ()
    # Other invoices on this PO whose net amount could not be read: billed_before is a floor.
    billed_unreadable_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContractPriceRecord:
    contract_id: str
    currency: str
    sku: str
    unit_price: Decimal


@dataclass(frozen=True)
class DuplicateCandidate:
    invoice_id: str
    invoice_number: str | None
    invoice_number_norm: str | None
    total: Decimal | None
    issue_date: date | None
    match: Literal["exact", "fuzzy"]
    similarity: float


class DocumentFinding(Protocol):
    """A hidden-text or instruction finding (ingestion's HiddenTextFinding has this shape)."""

    @property
    def kind(self) -> str: ...

    @property
    def page(self) -> int | None: ...

    @property
    def snippet(self) -> str: ...

    @property
    def evidence(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class RuleContext:
    # None: no vendor matched by VAT id, IBAN or a confident name match.
    vendor_match: VendorMatch | None = None
    bank_accounts: tuple[BankAccountRecord, ...] = ()
    # None: the invoice names no PO, or the PO does not exist.
    po: PurchaseOrderContext | None = None
    # Prices of the vendor's contracts valid on the invoice's issue date.
    contract_prices: tuple[ContractPriceRecord, ...] = ()
    vat_rates: Mapping[str, Decimal] = field(default_factory=dict)
    po_tolerance_pct: Decimal | None = None
    duplicates: tuple[DuplicateCandidate, ...] = ()
    document_findings: tuple[DocumentFinding, ...] = ()
    text_sweep: TextSweep = field(default_factory=TextSweep)

    @property
    def vendor(self) -> VendorRecord | None:
        return self.vendor_match.vendor if self.vendor_match is not None else None
