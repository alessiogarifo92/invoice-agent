"""Clean invoices: one per purchase order, consistent with the PO, contract and master data."""

import random
import re
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from synth.model import Invoice, InvoiceFields, InvoiceLine, MasterData, money

# Vendor invoice-number styles, assigned per vendor in order. "{y}" is the issue year.
NUMBER_FORMATS: tuple[str, ...] = (
    "INV-{n:04d}",
    "{y}/{n:03d}",
    "F-{y}-{n:04d}",
    "RE-{n:05d}",
    "FAC-{y}-{n:03d}",
)


def invoice_id(n: int) -> str:
    return f"INV-SEED-{n:03d}"


def reformat_invoice_number(number: str) -> str:
    """The same number written differently: separators become spaces, zero padding is dropped.

    "INV-0042" -> "INV 42". A resent duplicate uses this to dodge an exact-match check.
    """
    groups = re.findall(r"[A-Za-z]+|\d+", number)
    return " ".join(str(int(g)) if g.isdigit() else g for g in groups)


def normalize_invoice_number(number: str) -> str:
    """Comparison key: uppercase alphanumeric groups, numeric groups without leading zeros."""
    groups = re.findall(r"[A-Za-z]+|\d+", number)
    return "".join(str(int(g)) if g.isdigit() else g.upper() for g in groups)


def priced(
    fields: InvoiceFields, lines: tuple[InvoiceLine, ...], vat_rate: Decimal
) -> InvoiceFields:
    """`fields` with new lines and VAT rate, and subtotal, VAT amount and total recomputed."""
    subtotal = money(sum((ln.line_total for ln in lines), Decimal(0)))
    vat_amount = money(subtotal * vat_rate / 100)
    return replace(
        fields,
        lines=lines,
        subtotal=subtotal,
        vat_rate=vat_rate,
        vat_amount=vat_amount,
        total=subtotal + vat_amount,
    )


def build_clean_invoices(master: MasterData, rng: random.Random) -> list[Invoice]:
    """One clean invoice per PO, ids assigned in issue-date order."""
    drafts: list[tuple[InvoiceFields, int]] = []
    for po in master.purchase_orders:
        vendor = master.vendor(po.vendor_id)
        contract = master.contract_for(vendor.id)
        issue_date = po.issue_date + timedelta(days=rng.randint(5, 25))
        lines = tuple(
            InvoiceLine(ln.line_no, ln.sku, ln.description, ln.quantity, ln.unit_price)
            for ln in po.lines
        )
        empty = InvoiceFields(
            vendor_name=vendor.name,
            vendor_vat_id=vendor.vat_id,
            vendor_country=vendor.country,
            vendor_address=vendor.address,
            invoice_number="",
            issue_date=issue_date,
            due_date=issue_date + timedelta(days=contract.payment_days),
            currency=po.currency,
            po_number=po.id,
            iban=master.current_iban(vendor.id),
            lines=(),
            subtotal=Decimal(0),
            vat_rate=Decimal(0),
            vat_amount=Decimal(0),
            total=Decimal(0),
        )
        drafts.append(
            (priced(empty, lines, master.vat_rates[vendor.country]), contract.payment_days)
        )

    drafts.sort(key=lambda d: (d[0].issue_date, d[0].po_number))

    # Per-vendor running invoice numbers, in issue-date order.
    counters = {v.id: rng.randint(10, 400) for v in master.vendors}
    styles = {v.id: NUMBER_FORMATS[i % len(NUMBER_FORMATS)] for i, v in enumerate(master.vendors)}
    out: list[Invoice] = []
    for n, (fields, payment_days) in enumerate(drafts, start=1):
        vendor_id = master.purchase_order(fields.po_number).vendor_id
        counters[vendor_id] += rng.randint(1, 9)
        number = styles[vendor_id].format(n=counters[vendor_id], y=fields.issue_date.year)
        out.append(Invoice(invoice_id(n), replace(fields, invoice_number=number), payment_days))
    return out
