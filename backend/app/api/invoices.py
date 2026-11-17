import hashlib
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import Connection

from app.api.auth import DemoToken, require_demo_token
from app.api.common import ErrorResponse, error_response, get_app_settings
from app.config import Settings
from app.db import get_connection
from app.ingestion import store
from app.ingestion.upload import (
    UploadRejected,
    check_content_type,
    check_magic,
    check_size,
    inspect_pdf,
)
from app.jobs import queue
from app.review.store import current_proposal_id

router = APIRouter(prefix="/invoices", tags=["invoices"])

FILENAME_MAX = 255
DEFAULT_FILENAME = "upload.pdf"


class InvoiceAccepted(BaseModel):
    id: UUID
    status: str
    sha256: str


class JobState(BaseModel):
    """The invoice's newest job, for polling. `run_after`: when a queued job is due (a retry
    backoff, or the quota reset for an invoice in awaiting_quota)."""

    status: str
    stage: str
    attempts: int
    run_after: datetime


class InvoiceSummary(BaseModel):
    id: UUID
    status: str
    sha256: str
    filename: str
    page_count: int
    created_at: datetime
    job: JobState | None
    # The newest proposal (GET /proposals/{id}); None until the rules stage has run.
    proposal_id: UUID | None


class InvoiceCard(BaseModel):
    """A gallery entry (DESIGN.md, "Case card"). The printed values come from the newest
    extraction, as printed (`total` is a decimal string); all None before extraction."""

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


class InvoicePage(BaseModel):
    items: list[InvoiceCard]
    total: int
    limit: int
    offset: int


def _base_name(filename: str | None) -> str:
    # Only the last path segment of the client's name is kept, whatever its separators.
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name[:FILENAME_MAX] or DEFAULT_FILENAME


def _accepted(record: store.InvoiceRecord) -> InvoiceAccepted:
    return InvoiceAccepted(id=record.id, status=record.status, sha256=record.sha256)


@router.post(
    "",
    status_code=202,
    response_model=InvoiceAccepted,
    responses={
        200: {"model": InvoiceAccepted, "description": "Same bytes already uploaded"},
        401: {"model": ErrorResponse},
        413: {"model": ErrorResponse},
        415: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)
async def upload_invoice(
    file: UploadFile,
    response: Response,
    conn: Annotated[Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    _token: Annotated[DemoToken, Depends(require_demo_token)],
) -> InvoiceAccepted | JSONResponse:
    """Accept one PDF (202), or return the invoice already stored for the same bytes (200).
    Needs the demo token (401 otherwise).

    Async, so the event loop owns only the cheap work: PDF parsing and every DB call run in
    the thread pool.
    """
    try:
        check_content_type(file.content_type)
        # Never more than limit + 1 bytes in memory: one extra byte proves the file too big.
        data = await file.read(settings.max_upload_bytes + 1)
        check_size(data, settings.max_upload_bytes)
        check_magic(data)
    except UploadRejected as exc:
        return error_response(exc.status_code, exc.code, exc.message)

    sha256 = hashlib.sha256(data).hexdigest()
    existing = await run_in_threadpool(store.find_by_sha256, conn, sha256)
    if existing is not None:
        response.status_code = 200
        return _accepted(existing)

    try:
        page_count = await run_in_threadpool(inspect_pdf, data, settings.max_pages)
    except UploadRejected as exc:
        return error_response(exc.status_code, exc.code, exc.message)

    record, created = await run_in_threadpool(
        store.store_invoice, conn, data, sha256, _base_name(file.filename), page_count
    )
    if not created:
        response.status_code = 200
    return _accepted(record)


@router.get("", response_model=InvoicePage)
def list_invoices(
    conn: Annotated[Connection, Depends(get_connection)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> InvoicePage:
    """The gallery: a page of invoices, newest first."""
    items, total = store.list_invoices(conn, limit, offset)
    return InvoicePage(
        items=[
            InvoiceCard(
                id=i.id,
                status=i.status,
                filename=i.filename,
                created_at=i.created_at,
                proposal_id=i.proposal_id,
                final_action=i.final_action,
                vendor_name=i.vendor_name,
                invoice_number=i.invoice_number,
                total=i.total,
                currency=i.currency,
            )
            for i in items
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{invoice_id}", response_model=InvoiceSummary, responses={404: {"model": ErrorResponse}}
)
def get_invoice(
    invoice_id: UUID, conn: Annotated[Connection, Depends(get_connection)]
) -> InvoiceSummary | JSONResponse:
    """The invoice and its job's progress; the UI polls this while the worker runs."""
    record = store.get_invoice(conn, invoice_id)
    if record is None:
        return error_response(404, "not_found", f"invoice {invoice_id} does not exist")
    job = queue.latest_job(conn, invoice_id)
    return InvoiceSummary(
        id=record.id,
        status=record.status,
        sha256=record.sha256,
        filename=record.filename,
        page_count=record.page_count,
        created_at=record.created_at,
        job=None
        if job is None
        else JobState(
            status=job.status, stage=job.stage, attempts=job.attempts, run_after=job.run_after
        ),
        proposal_id=current_proposal_id(conn, invoice_id),
    )
