"""Load the synthetic master data (data/seed/master.json) with an idempotent upsert.

Runs on the OWNER connection (the app role can only read master data). Re-running it
with the same file changes nothing; a changed value in the file overwrites the row.
Rows that are no longer in the file are left in place.

Usage:
    OWNER_DATABASE_URL=... uv run python scripts/load_master_data.py [path/to/master.json]
"""

import json
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Self

import psycopg
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "seed" / "master.json"


class MasterDataError(Exception):
    """The master-data file is unreadable or does not match the contract."""


class _Strict(BaseModel):
    # Unknown keys are rejected: a typo in the generator must not be silently dropped.
    model_config = ConfigDict(extra="forbid", frozen=True)


class Vendor(_Strict):
    id: str
    name: str
    vat_id: str
    country: str
    address: str


class BankAccount(_Strict):
    vendor_id: str
    iban: str
    valid_from: date
    valid_to: date | None


class PoLine(_Strict):
    line_no: int
    sku: str
    description: str
    quantity: Decimal
    unit_price: Decimal


class PurchaseOrder(_Strict):
    id: str
    vendor_id: str
    currency: str
    issue_date: date
    total_net: Decimal
    lines: list[PoLine]


class ContractPrice(_Strict):
    sku: str
    unit_price: Decimal


class ContractClause(_Strict):
    clause_no: str
    title: str
    text: str


class Contract(_Strict):
    id: str
    vendor_id: str
    currency: str
    valid_from: date
    valid_to: date
    prices: list[ContractPrice]
    clauses: list[ContractClause]


class MasterData(_Strict):
    vendors: list[Vendor]
    vendor_bank_accounts: list[BankAccount]
    purchase_orders: list[PurchaseOrder]
    contracts: list[Contract]
    vat_rates: dict[str, Decimal]
    po_tolerance_pct: Decimal

    @model_validator(mode="after")
    def _vendors_exist(self) -> Self:
        known = {v.id for v in self.vendors}
        refs = [
            *(
                ("vendor_bank_accounts", i, a.vendor_id)
                for i, a in enumerate(self.vendor_bank_accounts)
            ),
            *(("purchase_orders", i, p.vendor_id) for i, p in enumerate(self.purchase_orders)),
            *(("contracts", i, c.vendor_id) for i, c in enumerate(self.contracts)),
        ]
        for section, index, vendor_id in refs:
            if vendor_id not in known:
                raise ValueError(f"{section}.{index}.vendor_id {vendor_id!r} is not in vendors")
        return self


def read_master(path: Path) -> MasterData:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MasterDataError(f"{path}: cannot read: {exc.strerror}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MasterDataError(
            f"{path}: invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"
        ) from exc
    try:
        return MasterData.model_validate(document)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        raise MasterDataError(
            f"{path}: does not match the master-data contract: {problems}"
        ) from exc


def load(conn: psycopg.Connection, data: MasterData) -> dict[str, int]:
    """Upsert every row in one transaction. Returns the number of rows written per table."""
    po_lines = [(po.id, line) for po in data.purchase_orders for line in po.lines]
    prices = [(c.id, p) for c in data.contracts for p in c.prices]
    clauses = [(c.id, cl) for c in data.contracts for cl in c.clauses]
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO vendors (id, name, vat_id, country, address) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, vat_id = EXCLUDED.vat_id, "
            "country = EXCLUDED.country, address = EXCLUDED.address",
            [(v.id, v.name, v.vat_id, v.country, v.address) for v in data.vendors],
        )
        cur.executemany(
            "INSERT INTO vendor_bank_accounts (vendor_id, iban, valid_from, valid_to) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT ON CONSTRAINT vendor_bank_accounts_natural_key "
            "DO UPDATE SET valid_to = EXCLUDED.valid_to",
            [(a.vendor_id, a.iban, a.valid_from, a.valid_to) for a in data.vendor_bank_accounts],
        )
        cur.executemany(
            "INSERT INTO purchase_orders (id, vendor_id, currency, issue_date, total_net) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET "
            "vendor_id = EXCLUDED.vendor_id, currency = EXCLUDED.currency, "
            "issue_date = EXCLUDED.issue_date, total_net = EXCLUDED.total_net",
            [
                (p.id, p.vendor_id, p.currency, p.issue_date, p.total_net)
                for p in data.purchase_orders
            ],
        )
        cur.executemany(
            "INSERT INTO po_lines (po_id, line_no, sku, description, quantity, unit_price) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (po_id, line_no) DO UPDATE SET "
            "sku = EXCLUDED.sku, description = EXCLUDED.description, "
            "quantity = EXCLUDED.quantity, unit_price = EXCLUDED.unit_price",
            [
                (po_id, ln.line_no, ln.sku, ln.description, ln.quantity, ln.unit_price)
                for po_id, ln in po_lines
            ],
        )
        cur.executemany(
            "INSERT INTO contracts (id, vendor_id, currency, valid_from, valid_to) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET "
            "vendor_id = EXCLUDED.vendor_id, currency = EXCLUDED.currency, "
            "valid_from = EXCLUDED.valid_from, valid_to = EXCLUDED.valid_to",
            [(c.id, c.vendor_id, c.currency, c.valid_from, c.valid_to) for c in data.contracts],
        )
        cur.executemany(
            "INSERT INTO contract_prices (contract_id, sku, unit_price) VALUES (%s, %s, %s) "
            "ON CONFLICT (contract_id, sku) DO UPDATE SET unit_price = EXCLUDED.unit_price",
            [(contract_id, p.sku, p.unit_price) for contract_id, p in prices],
        )
        cur.executemany(
            'INSERT INTO contract_clauses (contract_id, clause_no, title, "text") '
            "VALUES (%s, %s, %s, %s) ON CONFLICT (contract_id, clause_no) DO UPDATE SET "
            'title = EXCLUDED.title, "text" = EXCLUDED."text"',
            [(contract_id, cl.clause_no, cl.title, cl.text) for contract_id, cl in clauses],
        )
        cur.executemany(
            "INSERT INTO vat_rates (country, rate_pct) VALUES (%s, %s) "
            "ON CONFLICT (country) DO UPDATE SET rate_pct = EXCLUDED.rate_pct",
            list(data.vat_rates.items()),
        )
        cur.execute(
            "INSERT INTO settings_kv (key, value) VALUES ('po_tolerance_pct', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (str(data.po_tolerance_pct),),
        )
    return {
        "vendors": len(data.vendors),
        "vendor_bank_accounts": len(data.vendor_bank_accounts),
        "purchase_orders": len(data.purchase_orders),
        "po_lines": len(po_lines),
        "contracts": len(data.contracts),
        "contract_prices": len(prices),
        "contract_clauses": len(clauses),
        "vat_rates": len(data.vat_rates),
        "settings_kv": 1,
    }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    url = os.environ.get("OWNER_DATABASE_URL")
    if not url:
        print("OWNER_DATABASE_URL is required", file=sys.stderr)
        return 2
    path = Path(args[0]) if args else DEFAULT_PATH
    try:
        data = read_master(path)
    except MasterDataError as exc:
        print(exc, file=sys.stderr)
        return 1
    with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1)) as conn:
        counts = load(conn, data)
    print(", ".join(f"{table}={n}" for table, n in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
