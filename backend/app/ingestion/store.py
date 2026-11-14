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
