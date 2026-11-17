"""In-memory matcher over master.json and the extractions added so far (the Layer-1 eval).

It answers exactly like `SqlMatcher` (same thresholds, pg_trgm's similarity, the latest
extraction per invoice), so the eval runs without a database and measures what the app does.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Self

from app.matching.base import (
    DUPLICATE_AMOUNT_TOLERANCE,
    DUPLICATE_DATE_WINDOW_DAYS,
    DUPLICATE_NUMBER_THRESHOLD,
    VENDOR_NAME_THRESHOLD,
    ExtractionRecord,
    compact_id,
    extraction_record,
)
from app.rules.context import (
    BankAccountRecord,
    ContractPriceRecord,
    DuplicateCandidate,
    POLineRecord,
    PurchaseOrderContext,
    VendorMatch,
    VendorRecord,
)
from app.rules.facts import parse_facts, to_decimal
from app.rules.numbers import at_least, trigram_similarity


@dataclass(frozen=True)
class _Contract:
    id: str
    vendor_id: str
    currency: str
    valid_from: date
    valid_to: date
    prices: tuple[tuple[str, Decimal], ...]


@dataclass(frozen=True)
class _Extraction:
    fields: Mapping[str, Any]
    record: ExtractionRecord


def _day(value: str | None) -> date | None:
    return date.fromisoformat(value) if value is not None else None


class MemoryMatcher:
    def __init__(
        self,
        vendors: list[VendorRecord],
        accounts: dict[str, list[BankAccountRecord]],
        purchase_orders: dict[str, PurchaseOrderContext],
        contracts: list[_Contract],
        vat_rates: dict[str, Decimal],
        po_tolerance_pct: Decimal | None,
    ) -> None:
        self._vendors = sorted(vendors, key=lambda v: v.id)
        self._accounts = accounts
        self._pos = purchase_orders
        self._contracts = sorted(contracts, key=lambda c: c.id)
        self._vat_rates = vat_rates
        self._tolerance = po_tolerance_pct
        # Latest extraction per invoice, in the order the invoices were first added.
        self._extractions: dict[str, _Extraction] = {}

    @classmethod
    def from_master(cls, master: Mapping[str, Any]) -> Self:
        """From a master-data document shaped like data/seed/master.json."""
        vendors = [
            VendorRecord(v["id"], v["name"], v["vat_id"], v["country"]) for v in master["vendors"]
        ]
        accounts: dict[str, list[BankAccountRecord]] = {}
        for a in master["vendor_bank_accounts"]:
            record = BankAccountRecord(
                a["iban"], date.fromisoformat(a["valid_from"]), _day(a["valid_to"])
            )
            accounts.setdefault(a["vendor_id"], []).append(record)
        for records in accounts.values():
            records.sort(key=lambda r: (r.valid_from, r.iban))
        pos = {
            p["id"]: PurchaseOrderContext(
                id=p["id"],
                vendor_id=p["vendor_id"],
                currency=p["currency"],
                issue_date=date.fromisoformat(p["issue_date"]),
                total_net=Decimal(p["total_net"]),
                lines=tuple(
                    POLineRecord(
                        ln["line_no"], ln["sku"], Decimal(ln["quantity"]), Decimal(ln["unit_price"])
                    )
                    for ln in sorted(p["lines"], key=lambda ln: ln["line_no"])
                ),
            )
            for p in master["purchase_orders"]
        }
        contracts = [
            _Contract(
                c["id"],
                c["vendor_id"],
                c["currency"],
                date.fromisoformat(c["valid_from"]),
                date.fromisoformat(c["valid_to"]),
                tuple(sorted((p["sku"], Decimal(p["unit_price"])) for p in c["prices"])),
            )
            for c in master["contracts"]
        ]
        rates = {country: Decimal(rate) for country, rate in master["vat_rates"].items()}
        return cls(
            vendors, accounts, pos, contracts, rates, to_decimal(master.get("po_tolerance_pct"))
        )

    @classmethod
    def from_master_file(cls, path: Path) -> Self:
        return cls.from_master(json.loads(path.read_text(encoding="utf-8")))

    def add_extraction(self, invoice_id: str, fields: Mapping[str, Any]) -> None:
        """Record an invoice's extraction; a later one for the same invoice replaces it."""
        self._extractions[invoice_id] = _Extraction(fields, extraction_record(fields))

    def _vendor(self, vendor_id: str) -> VendorRecord | None:
        return next((v for v in self._vendors if v.id == vendor_id), None)

    def find_vendor(
        self, vat_id: str | None, iban: str | None, name: str | None
    ) -> VendorMatch | None:
        if vat_id and (vat := compact_id(vat_id)):
            for vendor in self._vendors:
                if vendor.vat_id == vat:
                    return VendorMatch(vendor, "vat_id", 1.0)
        if iban and (compact := compact_id(iban)):
            for vendor in self._vendors:
                if any(a.iban == compact for a in self._accounts.get(vendor.id, [])):
                    return VendorMatch(vendor, "iban", 1.0)
        if name and name.strip():
            scored = [(trigram_similarity(v.name, name), v) for v in self._vendors]
            scored = [(s, v) for s, v in scored if at_least(s, VENDOR_NAME_THRESHOLD)]
            if scored:
                score, vendor = min(scored, key=lambda sv: (-sv[0], sv[1].id))
                return VendorMatch(vendor, "name", score)
        return None

    def bank_accounts(self, vendor_id: str) -> tuple[BankAccountRecord, ...]:
        return tuple(self._accounts.get(vendor_id, []))

    def get_po_context(
        self, po_id: str, exclude_invoice_id: str | None = None
    ) -> PurchaseOrderContext | None:
        po = self._pos.get(po_id)
        if po is None:
            return None
        billed = Decimal(0)
        billed_ids: list[str] = []
        unreadable: list[str] = []
        for invoice_id in sorted(self._extractions):
            if invoice_id == exclude_invoice_id:
                continue
            facts = parse_facts(self._extractions[invoice_id].fields)
            if facts.po_number != po_id:
                continue
            if facts.subtotal is None:
                unreadable.append(invoice_id)
            else:
                billed += facts.subtotal
                billed_ids.append(invoice_id)
        return PurchaseOrderContext(
            id=po.id,
            vendor_id=po.vendor_id,
            currency=po.currency,
            issue_date=po.issue_date,
            total_net=po.total_net,
            lines=po.lines,
            billed_before=billed,
            billed_invoice_ids=tuple(billed_ids),
            billed_unreadable_ids=tuple(unreadable),
        )

    def contract_prices(self, vendor_id: str, on_date: date) -> tuple[ContractPriceRecord, ...]:
        return tuple(
            ContractPriceRecord(c.id, c.currency, sku, price)
            for c in self._contracts
            if c.vendor_id == vendor_id and c.valid_from <= on_date <= c.valid_to
            for sku, price in c.prices
        )

    def find_duplicates(
        self,
        vendor_id: str,
        invoice_number_norm: str,
        total: Decimal | None,
        issue_date: date | None,
        exclude_invoice_id: str | None = None,
    ) -> tuple[DuplicateCandidate, ...]:
        vendor = self._vendor(vendor_id)
        if vendor is None or not invoice_number_norm:
            return ()
        found = []
        for invoice_id in sorted(self._extractions):
            record = self._extractions[invoice_id].record
            if invoice_id == exclude_invoice_id or record.vendor_vat_id != vendor.vat_id:
                continue
            if record.invoice_number_norm is None:
                continue
            similarity = trigram_similarity(record.invoice_number_norm, invoice_number_norm)
            match: Literal["exact", "fuzzy"]
            if record.invoice_number_norm == invoice_number_norm:
                match = "exact"
            elif (
                total is not None
                and issue_date is not None
                and record.total is not None
                and record.issue_date is not None
                and abs(record.total - total) <= DUPLICATE_AMOUNT_TOLERANCE
                and abs((record.issue_date - issue_date).days) <= DUPLICATE_DATE_WINDOW_DAYS
                and at_least(similarity, DUPLICATE_NUMBER_THRESHOLD)
            ):
                match = "fuzzy"
            else:
                continue
            found.append(
                DuplicateCandidate(
                    invoice_id=invoice_id,
                    invoice_number=record.invoice_number,
                    invoice_number_norm=record.invoice_number_norm,
                    total=record.total,
                    issue_date=record.issue_date,
                    match=match,
                    similarity=similarity,
                )
            )
        return tuple(found)

    def vat_rates(self) -> dict[str, Decimal]:
        return dict(self._vat_rates)

    def po_tolerance_pct(self) -> Decimal | None:
        return self._tolerance
