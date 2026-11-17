"""Plain SQL lookups over the master data and earlier extractions (B1 retrieval, no vectors).

Every function runs in the caller's transaction and only reads, so it works on the
restricted app role. `SqlMatcher` binds them to one connection behind the `Matcher` Protocol.

"Latest extraction" means the newest row per invoice: extractions are not unique, and a
re-run must replace, not add to, what an invoice billed or which number it carries.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, Row, text

from app.matching.base import (
    CLAUSE_SEARCH_LIMIT,
    DUPLICATE_AMOUNT_TOLERANCE,
    DUPLICATE_DATE_WINDOW_DAYS,
    DUPLICATE_NUMBER_THRESHOLD,
    VENDOR_NAME_THRESHOLD,
    compact_id,
)
from app.rules.context import (
    BankAccountRecord,
    ContractPriceRecord,
    DuplicateCandidate,
    POLineRecord,
    PurchaseOrderContext,
    VendorMatch,
    VendorMatchMethod,
    VendorRecord,
)
from app.rules.facts import parse_facts, to_decimal

_VENDOR_COLUMNS = "v.id, v.name, v.vat_id, v.country"
# An extraction row that no newer row of the same invoice supersedes.
_LATEST = (
    "NOT EXISTS (SELECT 1 FROM extractions newer WHERE newer.invoice_id = e.invoice_id "
    "AND (newer.created_at, newer.id) > (e.created_at, e.id))"
)
_NOT_EXCLUDED = "(CAST(:exclude AS uuid) IS NULL OR e.invoice_id <> CAST(:exclude AS uuid))"


def _exact(real_expr: str) -> str:
    """A `real` read back exactly: psycopg parses `real` from its rounded text form."""
    return f"CAST({real_expr} AS double precision)"


@dataclass(frozen=True)
class ClauseHit:
    contract_id: str
    clause_no: str
    title: str
    text: str
    rank: float


def _vendor_match(row: Row[Any] | None, method: VendorMatchMethod) -> VendorMatch | None:
    if row is None:
        return None
    vendor = VendorRecord(row.id, row.name, row.vat_id, row.country)
    return VendorMatch(vendor, method, float(row.score))


def find_vendor(
    conn: Connection, vat_id: str | None, iban: str | None, name: str | None
) -> VendorMatch | None:
    """Exact VAT id, then exact IBAN on any account (past ones too), then pg_trgm on the name."""
    if vat_id and (vat := compact_id(vat_id)):
        row = conn.execute(
            text(f"SELECT {_VENDOR_COLUMNS}, 1.0 AS score FROM vendors v WHERE v.vat_id = :vat"),
            {"vat": vat},
        ).one_or_none()
        if row is not None:
            return _vendor_match(row, "vat_id")
    if iban and (compact := compact_id(iban)):
        row = conn.execute(
            text(
                f"SELECT DISTINCT {_VENDOR_COLUMNS}, 1.0 AS score FROM vendors v "
                "JOIN vendor_bank_accounts a ON a.vendor_id = v.id WHERE a.iban = :iban "
                "ORDER BY v.id LIMIT 1"
            ),
            {"iban": compact},
        ).one_or_none()
        if row is not None:
            return _vendor_match(row, "iban")
    if name and name.strip():
        # Compared as `real`, the type similarity() returns, so the in-memory matcher agrees.
        row = conn.execute(
            text(
                f"SELECT {_VENDOR_COLUMNS}, {_exact('similarity(v.name, :name)')} AS score "
                "FROM vendors v "
                "WHERE similarity(v.name, :name) >= CAST(:threshold AS real) "
                "ORDER BY score DESC, v.id LIMIT 1"
            ),
            {"name": name, "threshold": VENDOR_NAME_THRESHOLD},
        ).one_or_none()
        return _vendor_match(row, "name")
    return None


def bank_accounts(conn: Connection, vendor_id: str) -> tuple[BankAccountRecord, ...]:
    rows = conn.execute(
        text(
            "SELECT iban, valid_from, valid_to FROM vendor_bank_accounts "
            "WHERE vendor_id = :vendor_id ORDER BY valid_from, iban"
        ),
        {"vendor_id": vendor_id},
    ).all()
    return tuple(BankAccountRecord(r.iban, r.valid_from, r.valid_to) for r in rows)


def get_po_context(
    conn: Connection, po_id: str, exclude_invoice_id: str | None = None
) -> PurchaseOrderContext | None:
    """The PO, its lines, and the net billed on it by other invoices' latest extractions."""
    po = conn.execute(
        text(
            "SELECT id, vendor_id, currency, issue_date, total_net FROM purchase_orders "
            "WHERE id = :po_id"
        ),
        {"po_id": po_id},
    ).one_or_none()
    if po is None:
        return None
    lines = conn.execute(
        text(
            "SELECT line_no, sku, quantity, unit_price FROM po_lines WHERE po_id = :po_id "
            "ORDER BY line_no"
        ),
        {"po_id": po_id},
    ).all()
    billed_rows = conn.execute(
        text(
            "SELECT e.invoice_id, e.fields FROM extractions e "
            f"WHERE btrim(e.fields ->> 'po_number') = :po_id AND {_NOT_EXCLUDED} AND {_LATEST} "
            "ORDER BY e.invoice_id"
        ),
        {"po_id": po_id, "exclude": exclude_invoice_id},
    ).all()
    billed = Decimal(0)
    billed_ids: list[str] = []
    unreadable: list[str] = []
    for row in billed_rows:
        facts = parse_facts(row.fields)
        if facts.po_number != po_id:
            continue
        if facts.subtotal is None:
            unreadable.append(str(row.invoice_id))
        else:
            billed += facts.subtotal
            billed_ids.append(str(row.invoice_id))
    return PurchaseOrderContext(
        id=po.id,
        vendor_id=po.vendor_id,
        currency=po.currency,
        issue_date=po.issue_date,
        total_net=po.total_net,
        lines=tuple(POLineRecord(r.line_no, r.sku, r.quantity, r.unit_price) for r in lines),
        billed_before=billed,
        billed_invoice_ids=tuple(billed_ids),
        billed_unreadable_ids=tuple(unreadable),
    )


def contract_prices(
    conn: Connection, vendor_id: str, on_date: date
) -> tuple[ContractPriceRecord, ...]:
    rows = conn.execute(
        text(
            "SELECT c.id, c.currency, p.sku, p.unit_price FROM contracts c "
            "JOIN contract_prices p ON p.contract_id = c.id "
            "WHERE c.vendor_id = :vendor_id AND c.valid_from <= :on_date "
            "AND c.valid_to >= :on_date ORDER BY c.id, p.sku"
        ),
        {"vendor_id": vendor_id, "on_date": on_date},
    ).all()
    return tuple(ContractPriceRecord(r.id, r.currency, r.sku, r.unit_price) for r in rows)


def find_duplicates(
    conn: Connection,
    vendor_id: str,
    invoice_number_norm: str,
    total: Decimal | None,
    issue_date: date | None,
    exclude_invoice_id: str | None = None,
) -> tuple[DuplicateCandidate, ...]:
    """Exact (vendor, normalised number), or fuzzy: amount within a cent, issue dates at most
    10 days apart and a trigram-similar number. The vendor is its master-data VAT id, which
    is how `extractions` stores it (compacted)."""
    if not invoice_number_norm:
        return ()
    rows = conn.execute(
        text(
            "SELECT e.invoice_id, e.invoice_number, e.invoice_number_norm, e.total, "
            f"e.issue_date, {_exact('similarity(e.invoice_number_norm, :norm)')} AS similarity "
            "FROM extractions e JOIN vendors v ON v.vat_id = e.vendor_vat_id "
            f"WHERE v.id = :vendor_id AND {_NOT_EXCLUDED} AND {_LATEST} AND ("
            "e.invoice_number_norm = :norm OR ("
            "abs(e.total - CAST(:total AS numeric)) <= CAST(:amount_tol AS numeric) "
            "AND abs(e.issue_date - CAST(:issue_date AS date)) <= :days "
            "AND similarity(e.invoice_number_norm, :norm) >= CAST(:threshold AS real))) "
            "ORDER BY e.invoice_id"
        ),
        {
            "norm": invoice_number_norm,
            "vendor_id": vendor_id,
            "exclude": exclude_invoice_id,
            "total": total,
            "issue_date": issue_date,
            "amount_tol": DUPLICATE_AMOUNT_TOLERANCE,
            "days": DUPLICATE_DATE_WINDOW_DAYS,
            "threshold": DUPLICATE_NUMBER_THRESHOLD,
        },
    ).all()
    return tuple(
        DuplicateCandidate(
            invoice_id=str(r.invoice_id),
            invoice_number=r.invoice_number,
            invoice_number_norm=r.invoice_number_norm,
            total=r.total,
            issue_date=r.issue_date,
            match="exact" if r.invoice_number_norm == invoice_number_norm else "fuzzy",
            similarity=float(r.similarity),
        )
        for r in rows
    )


def vat_rates(conn: Connection) -> dict[str, Decimal]:
    rows = conn.execute(text("SELECT country, rate_pct FROM vat_rates ORDER BY country")).all()
    return {r.country: r.rate_pct for r in rows}


def po_tolerance_pct(conn: Connection) -> Decimal | None:
    value = conn.execute(
        text("SELECT value FROM settings_kv WHERE key = 'po_tolerance_pct'")
    ).scalar_one_or_none()
    return to_decimal(value)


def search_contract_clauses(conn: Connection, vendor_id: str, query: str) -> tuple[ClauseHit, ...]:
    """The vendor's contract clauses matching `query` (English full-text), best 5 by ts_rank.

    SQL only (the T8 agent tool): the in-memory matcher has no Postgres text search to mirror.
    """
    rows = conn.execute(
        text(
            'SELECT cl.contract_id, cl.clause_no, cl.title, cl."text", '
            "ts_rank(cl.tsv, q.query) AS rank "
            "FROM contract_clauses cl JOIN contracts c ON c.id = cl.contract_id, "
            "plainto_tsquery('english', :query) AS q(query) "
            "WHERE c.vendor_id = :vendor_id AND cl.tsv @@ q.query "
            "ORDER BY rank DESC, cl.contract_id, cl.clause_no LIMIT :limit"
        ),
        {"vendor_id": vendor_id, "query": query, "limit": CLAUSE_SEARCH_LIMIT},
    ).all()
    return tuple(
        ClauseHit(r.contract_id, r.clause_no, r.title, r.text, float(r.rank)) for r in rows
    )


class SqlMatcher:
    """The `Matcher` Protocol over one connection (the caller owns the transaction)."""

    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    def find_vendor(
        self, vat_id: str | None, iban: str | None, name: str | None
    ) -> VendorMatch | None:
        return find_vendor(self.conn, vat_id, iban, name)

    def bank_accounts(self, vendor_id: str) -> tuple[BankAccountRecord, ...]:
        return bank_accounts(self.conn, vendor_id)

    def get_po_context(
        self, po_id: str, exclude_invoice_id: str | None = None
    ) -> PurchaseOrderContext | None:
        return get_po_context(self.conn, po_id, exclude_invoice_id)

    def contract_prices(self, vendor_id: str, on_date: date) -> tuple[ContractPriceRecord, ...]:
        return contract_prices(self.conn, vendor_id, on_date)

    def find_duplicates(
        self,
        vendor_id: str,
        invoice_number_norm: str,
        total: Decimal | None,
        issue_date: date | None,
        exclude_invoice_id: str | None = None,
    ) -> tuple[DuplicateCandidate, ...]:
        return find_duplicates(
            self.conn, vendor_id, invoice_number_norm, total, issue_date, exclude_invoice_id
        )

    def vat_rates(self) -> dict[str, Decimal]:
        return vat_rates(self.conn)

    def po_tolerance_pct(self) -> Decimal | None:
        return po_tolerance_pct(self.conn)
