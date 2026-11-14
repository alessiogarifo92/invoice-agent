import hashlib
from datetime import datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import Connection

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

router = APIRouter(prefix="/invoices", tags=["invoices"])

FILENAME_MAX = 255
DEFAULT_FILENAME = "upload.pdf"


class ErrorResponse(BaseModel):
    code: str
    message: str


class InvoiceAccepted(BaseModel):
    id: UUID
    status: str
    sha256: str


class InvoiceSummary(BaseModel):
    id: UUID
    status: str
    sha256: str
    filename: str
    page_count: int
    created_at: datetime


def get_app_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content=ErrorResponse(code=code, message=message).model_dump()
    )


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
) -> InvoiceAccepted | JSONResponse:
    """Accept one PDF (202), or return the invoice already stored for the same bytes (200).

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
        return _error(exc.status_code, exc.code, exc.message)

    sha256 = hashlib.sha256(data).hexdigest()
    existing = await run_in_threadpool(store.find_by_sha256, conn, sha256)
    if existing is not None:
        response.status_code = 200
        return _accepted(existing)

    try:
        page_count = await run_in_threadpool(inspect_pdf, data, settings.max_pages)
    except UploadRejected as exc:
        return _error(exc.status_code, exc.code, exc.message)

    record, created = await run_in_threadpool(
        store.store_invoice, conn, data, sha256, _base_name(file.filename), page_count
    )
    if not created:
        response.status_code = 200
    return _accepted(record)


@router.get(
    "/{invoice_id}", response_model=InvoiceSummary, responses={404: {"model": ErrorResponse}}
)
def get_invoice(
    invoice_id: UUID, conn: Annotated[Connection, Depends(get_connection)]
) -> InvoiceSummary | JSONResponse:
    record = store.get_invoice(conn, invoice_id)
    if record is None:
        return _error(404, "not_found", f"invoice {invoice_id} does not exist")
    return InvoiceSummary(
        id=record.id,
        status=record.status,
        sha256=record.sha256,
        filename=record.filename,
        page_count=record.page_count,
        created_at=record.created_at,
    )
