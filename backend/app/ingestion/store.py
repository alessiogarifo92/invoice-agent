"""invoices / jobs writes for an accepted upload, and the minimal reads behind the API."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Row, text

from app.audit import append_event

ACTOR = "api"
_COLUMNS = "id, status, sha256, filename, page_count, created_at"


@dataclass(frozen=True)
class InvoiceRecord:
    id: UUID
    status: str
    sha256: str
    filename: str
    page_count: int
    created_at: datetime


def _record(row: Row[Any]) -> InvoiceRecord:
    return InvoiceRecord(
        id=row.id,
        status=row.status,
        sha256=row.sha256,
        filename=row.filename,
        page_count=row.page_count,
        created_at=row.created_at,
    )


def _select_by_sha256(conn: Connection, sha256: str) -> InvoiceRecord | None:
    row = conn.execute(
        text(f"SELECT {_COLUMNS} FROM invoices WHERE sha256 = :sha256"), {"sha256": sha256}
    ).one_or_none()
    return None if row is None else _record(row)


def find_by_sha256(conn: Connection, sha256: str) -> InvoiceRecord | None:
    """Read in its own short transaction, leaving the connection free for store_invoice."""
    with conn.begin():
        return _select_by_sha256(conn, sha256)


def get_invoice(conn: Connection, invoice_id: UUID) -> InvoiceRecord | None:
    with conn.begin():
        row = conn.execute(
            text(f"SELECT {_COLUMNS} FROM invoices WHERE id = :id"), {"id": invoice_id}
        ).one_or_none()
    return None if row is None else _record(row)


def store_invoice(
    conn: Connection, pdf: bytes, sha256: str, filename: str, page_count: int
) -> tuple[InvoiceRecord, bool]:
    """One transaction: the invoice, its first job and the 'invoice.received' audit event.

    Returns (record, created). When another request stored the same bytes first, its row is
    returned with created=False and nothing else is written. ON CONFLICT DO NOTHING waits
    for the competing transaction, so under READ COMMITTED the follow-up SELECT sees its row.
    """
    with conn.begin():
        row = conn.execute(
            text(
                "INSERT INTO invoices (sha256, filename, pdf, size_bytes, page_count, status) "
                "VALUES (:sha256, :filename, :pdf, :size, :page_count, 'received') "
                f"ON CONFLICT (sha256) DO NOTHING RETURNING {_COLUMNS}"
            ),
            {
                "sha256": sha256,
                "filename": filename,
                "pdf": pdf,
                "size": len(pdf),
                "page_count": page_count,
            },
        ).one_or_none()
        if row is None:
            existing = _select_by_sha256(conn, sha256)
            if existing is None:  # pragma: no cover - the conflicting row cannot vanish
                raise RuntimeError(f"sha256 {sha256} conflicted but no invoice row is visible")
            return existing, False
        record = _record(row)
        conn.execute(
            text("INSERT INTO jobs (invoice_id, stage, status) VALUES (:id, 'extract', 'queued')"),
            {"id": record.id},
        )
        append_event(
            conn,
            ACTOR,
            "invoice.received",
            "invoice",
            str(record.id),
            {
                "sha256": sha256,
                "size": len(pdf),
                "page_count": page_count,
                "filename": filename,
            },
        )
    return record, True


@dataclass(frozen=True)
class InvoiceListItem:
    """One gallery card: the invoice, its newest proposal's action and the printed values of
    its newest extraction (all None until the worker has got that far)."""

    id: UUID
    status: str
    filename: str
    created_at: datetime
    proposal_id: UUID | None
    final_action: str | None
    vendor_name: str | None
    invoice_number: str | None
    total: str | None
    currency: str | None


def list_invoices(conn: Connection, limit: int, offset: int) -> tuple[list[InvoiceListItem], int]:
    """A page of invoices, newest first, and the total count, in one short transaction."""
    with conn.begin():
        total: int = conn.execute(text("SELECT count(*) FROM invoices")).scalar_one()
        page = conn.execute(
            text(
                "SELECT i.id, i.status, i.filename, i.created_at, p.id AS proposal_id, "
                "p.final_action, e.fields ->> 'vendor_name' AS vendor_name, "
                "e.fields ->> 'invoice_number' AS invoice_number, "
                "e.fields ->> 'total' AS total, e.fields ->> 'currency' AS currency "
                "FROM invoices i "
                "LEFT JOIN LATERAL (SELECT id, final_action FROM proposals "
                "WHERE invoice_id = i.id ORDER BY created_at DESC, id DESC LIMIT 1) p ON true "
                "LEFT JOIN LATERAL (SELECT fields FROM extractions "
                "WHERE invoice_id = i.id ORDER BY created_at DESC, id DESC LIMIT 1) e ON true "
                "ORDER BY i.created_at DESC, i.id DESC LIMIT :limit OFFSET :offset"
            ),
            {"limit": limit, "offset": offset},
        ).all()
    items = [
        InvoiceListItem(
            id=row.id,
            status=row.status,
            filename=row.filename,
            created_at=row.created_at,
            proposal_id=row.proposal_id,
            final_action=row.final_action,
            vendor_name=row.vendor_name,
            invoice_number=row.invoice_number,
            total=row.total,
            currency=row.currency,
        )
        for row in page
    ]
    return items, total
