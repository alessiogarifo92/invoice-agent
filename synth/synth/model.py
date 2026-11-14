"""Data model of the synthetic dataset and its JSON form (the output contract).

Money is `Decimal`, serialised as a string with two decimals; quantities are serialised as
plain integer strings; dates as ISO "YYYY-MM-DD".
"""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

CENT = Decimal("0.01")

Action = Literal["approve", "clarify", "block"]


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def fmt_money(value: Decimal) -> str:
    return str(money(value))


def fmt_qty(value: int) -> str:
    return str(value)


@dataclass(frozen=True)
class Vendor:
    id: str
    name: str
    vat_id: str
    country: str
    address: str


@dataclass(frozen=True)
class BankAccount:
    vendor_id: str
    iban: str
    valid_from: date
    valid_to: date | None


@dataclass(frozen=True)
class POLine:
    line_no: int
    sku: str
    description: str
    quantity: int
    unit_price: Decimal


@dataclass(frozen=True)
class PurchaseOrder:
    id: str
    vendor_id: str
    currency: str
    issue_date: date
    lines: tuple[POLine, ...]

    @property
    def total_net(self) -> Decimal:
        return money(sum((money(ln.unit_price * ln.quantity) for ln in self.lines), Decimal(0)))


@dataclass(frozen=True)
class ContractPrice:
    sku: str
    unit_price: Decimal


@dataclass(frozen=True)
class Clause:
    clause_no: str
    title: str
    text: str


@dataclass(frozen=True)
class Contract:
    id: str
    vendor_id: str
    currency: str
    valid_from: date
    valid_to: date
    payment_days: int
    prices: tuple[ContractPrice, ...]
    clauses: tuple[Clause, ...]

    def price_of(self, sku: str) -> Decimal:
        for p in self.prices:
            if p.sku == sku:
                return p.unit_price
        raise KeyError(sku)


@dataclass(frozen=True)
class MasterData:
    vendors: tuple[Vendor, ...]
    bank_accounts: tuple[BankAccount, ...]
    purchase_orders: tuple[PurchaseOrder, ...]
    contracts: tuple[Contract, ...]
    vat_rates: dict[str, Decimal]
    po_tolerance_pct: Decimal

    def vendor(self, vendor_id: str) -> Vendor:
        return next(v for v in self.vendors if v.id == vendor_id)

    def contract_for(self, vendor_id: str) -> Contract:
        return next(c for c in self.contracts if c.vendor_id == vendor_id)

    def purchase_order(self, po_id: str) -> PurchaseOrder:
        return next(p for p in self.purchase_orders if p.id == po_id)

    def current_iban(self, vendor_id: str) -> str:
        return next(
            a.iban for a in self.bank_accounts if a.vendor_id == vendor_id and a.valid_to is None
        )

    def past_ibans(self, vendor_id: str) -> list[str]:
        return [
            a.iban
            for a in self.bank_accounts
            if a.vendor_id == vendor_id and a.valid_to is not None
        ]


@dataclass(frozen=True)
class InvoiceLine:
    line_no: int
    sku: str
    description: str
    quantity: int
    unit_price: Decimal

    @property
    def line_total(self) -> Decimal:
        return money(self.unit_price * self.quantity)


@dataclass(frozen=True)
class InvoiceFields:
    """Exactly the values printed on the PDF (what an extractor should return)."""

    vendor_name: str
    vendor_vat_id: str
    vendor_country: str
    vendor_address: str
    invoice_number: str
    issue_date: date
    due_date: date
    currency: str
    po_number: str
    iban: str
    lines: tuple[InvoiceLine, ...]
    subtotal: Decimal
    vat_rate: Decimal
    vat_amount: Decimal
    total: Decimal


@dataclass(frozen=True)
class HiddenText:
    """Text drawn on the PDF so that a human reader does not see it."""

    kind: Literal["white", "tiny"]
    text: str


@dataclass(frozen=True)
class Invoice:
    invoice_id: str
    fields: InvoiceFields
    payment_days: int
    anomalies: tuple[str, ...] = ()
    duplicate_of: str | None = None
    notes: str = "Clean invoice: matches its PO, the contract prices and the vendor master data."
    # Render-only extras used by the injection mutators. They are not invoice fields.
    visible_note: str | None = None
    hidden_texts: tuple[HiddenText, ...] = ()
    metadata_injection: str | None = None

    @property
    def file(self) -> str:
        return f"{self.invoice_id}.pdf"

    @property
    def expected_action(self) -> Action:
        return expected_action(self.anomalies)


def expected_action(anomalies: tuple[str, ...]) -> Action:
    """Clean -> approve; a confirmed resend duplicate -> block; any other anomaly -> clarify."""
    if "resend_reformatted_number" in anomalies:
        return "block"
    if anomalies:
        return "clarify"
    return "approve"


# --- JSON form -----------------------------------------------------------------------------


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d is not None else None


def master_to_json(master: MasterData) -> dict[str, Any]:
    return {
        "vendors": [
            {
                "id": v.id,
                "name": v.name,
                "vat_id": v.vat_id,
                "country": v.country,
                "address": v.address,
            }
            for v in master.vendors
        ],
        "vendor_bank_accounts": [
            {
                "vendor_id": a.vendor_id,
                "iban": a.iban,
                "valid_from": _iso(a.valid_from),
                "valid_to": _iso(a.valid_to),
            }
            for a in master.bank_accounts
        ],
        "purchase_orders": [
            {
                "id": po.id,
                "vendor_id": po.vendor_id,
                "currency": po.currency,
                "issue_date": _iso(po.issue_date),
                "total_net": fmt_money(po.total_net),
                "lines": [
                    {
                        "line_no": ln.line_no,
                        "sku": ln.sku,
                        "description": ln.description,
                        "quantity": fmt_qty(ln.quantity),
                        "unit_price": fmt_money(ln.unit_price),
                    }
                    for ln in po.lines
                ],
            }
            for po in master.purchase_orders
        ],
        "contracts": [
            {
                "id": c.id,
                "vendor_id": c.vendor_id,
                "currency": c.currency,
                "valid_from": _iso(c.valid_from),
                "valid_to": _iso(c.valid_to),
                "prices": [{"sku": p.sku, "unit_price": fmt_money(p.unit_price)} for p in c.prices],
                "clauses": [
                    {"clause_no": cl.clause_no, "title": cl.title, "text": cl.text}
                    for cl in c.clauses
                ],
            }
            for c in master.contracts
        ],
        "vat_rates": {k: fmt_money(v) for k, v in master.vat_rates.items()},
        "po_tolerance_pct": fmt_money(master.po_tolerance_pct),
    }


def fields_to_json(f: InvoiceFields) -> dict[str, Any]:
    return {
        "vendor_name": f.vendor_name,
        "vendor_vat_id": f.vendor_vat_id,
        "vendor_country": f.vendor_country,
        "vendor_address": f.vendor_address,
        "invoice_number": f.invoice_number,
        "issue_date": _iso(f.issue_date),
        "due_date": _iso(f.due_date),
        "currency": f.currency,
        "po_number": f.po_number,
        "iban": f.iban,
        "lines": [
            {
                "line_no": ln.line_no,
                "sku": ln.sku,
                "description": ln.description,
                "quantity": fmt_qty(ln.quantity),
                "unit_price": fmt_money(ln.unit_price),
                "line_total": fmt_money(ln.line_total),
            }
            for ln in f.lines
        ],
        "subtotal": fmt_money(f.subtotal),
        "vat_rate": fmt_money(f.vat_rate),
        "vat_amount": fmt_money(f.vat_amount),
        "total": fmt_money(f.total),
    }


def invoice_to_json(inv: Invoice) -> dict[str, Any]:
    return {
        "invoice_id": inv.invoice_id,
        "file": inv.file,
        "fields": fields_to_json(inv.fields),
        "anomalies": list(inv.anomalies),
        "expected_action": inv.expected_action,
        "duplicate_of": inv.duplicate_of,
        "notes": inv.notes,
    }
