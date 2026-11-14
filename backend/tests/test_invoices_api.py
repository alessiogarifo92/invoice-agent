"""POST /invoices and GET /invoices/{id} against a scratch database, as the app role."""

import asyncio
import hashlib
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Connection, Engine, text
from starlette.datastructures import UploadFile

from app.api import invoices as invoices_api
from app.audit import verify_chain
from app.config import Settings
from app.db import get_connection
from app.ingestion import store
from app.ingestion.upload import inspect_pdf
from app.main import create_app
from tests.pdf_factory import drawing_only_pdf, text_pdf
from tests.support import SEED_INVOICES

SEED_PDF = SEED_INVOICES / "INV-SEED-001.pdf"


@pytest.fixture
def client(app_engine: Engine) -> Iterator[TestClient]:
    # The API runs as the restricted app role, like in production.
    def override() -> Iterator[Connection]:
        with app_engine.connect() as conn:
            yield conn

    app = create_app(Settings())
    app.dependency_overrides[get_connection] = override
    with TestClient(app) as test_client:
        yield test_client


def _upload(
    client: TestClient,
    data: bytes,
    filename: str = "invoice.pdf",
    content_type: str = "application/pdf",
) -> Response:
    response: Response = client.post("/invoices", files={"file": (filename, data, content_type)})
    return response


def _counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            "invoices": conn.execute(text("SELECT count(*) FROM invoices")).scalar_one(),
            "jobs": conn.execute(text("SELECT count(*) FROM jobs")).scalar_one(),
            "received": conn.execute(
                text("SELECT count(*) FROM audit_events WHERE event_type = 'invoice.received'")
            ).scalar_one(),
        }


def _assert_error(response: Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code
    assert isinstance(body["message"], str) and body["message"]


# --- validation --------------------------------------------------------------------------


def test_wrong_content_type_is_415(client: TestClient, owner_engine: Engine) -> None:
    _assert_error(_upload(client, text_pdf(), "a.txt", "text/plain"), 415, "not_pdf")
    assert _counts(owner_engine) == {"invoices": 0, "jobs": 0, "received": 0}


def test_pdf_content_type_without_pdf_magic_is_415(client: TestClient) -> None:
    _assert_error(_upload(client, b"PK\x03\x04 a zip in disguise"), 415, "not_pdf")


def test_upload_over_two_megabytes_is_413(client: TestClient, owner_engine: Engine) -> None:
    payload = b"%PDF-1.4\n" + b"0" * 2_000_000
    assert len(payload) > Settings().max_upload_bytes
    _assert_error(_upload(client, payload), 413, "too_large")
    assert _counts(owner_engine)["invoices"] == 0


def test_upload_exactly_at_the_limit_is_not_413(app_engine: Engine) -> None:
    pdf = text_pdf()
    app = create_app(Settings(max_upload_bytes=len(pdf)))

    def override() -> Iterator[Connection]:
        with app_engine.connect() as conn:
            yield conn

    app.dependency_overrides[get_connection] = override
    with TestClient(app) as c:
        assert _upload(c, pdf).status_code == 202
        _assert_error(_upload(c, pdf + b"\n"), 413, "too_large")


def test_upload_reads_at_most_limit_plus_one_bytes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sizes: list[int] = []
    # The request carries Starlette's UploadFile; FastAPI's class is only the annotation.
    real_read = UploadFile.read

    async def spy(self: Any, size: int = -1) -> bytes:
        sizes.append(size)
        result: bytes = await real_read(self, size)
        return result

    monkeypatch.setattr(UploadFile, "read", spy)
    _upload(client, b"%PDF-1.4\n" + b"0" * 2_500_000)
    assert sizes == [Settings().max_upload_bytes + 1]


def test_corrupt_pdf_is_422(client: TestClient) -> None:
    _assert_error(_upload(client, b"%PDF-1.4\nnot really a pdf\n%%EOF"), 422, "unreadable_pdf")


def test_six_page_pdf_is_422_page_limit(client: TestClient, owner_engine: Engine) -> None:
    _assert_error(_upload(client, text_pdf(pages=6)), 422, "page_limit")
    assert _counts(owner_engine)["invoices"] == 0


def test_five_page_pdf_is_accepted(client: TestClient) -> None:
    response = _upload(client, text_pdf(pages=5))
    assert response.status_code == 202
    assert client.get(f"/invoices/{response.json()['id']}").json()["page_count"] == 5


def test_pdf_without_text_layer_is_422(client: TestClient) -> None:
    _assert_error(_upload(client, drawing_only_pdf()), 422, "no_text_layer")


# --- happy path and idempotency ----------------------------------------------------------


def test_seed_invoice_upload_stores_invoice_job_and_audit_event(
    client: TestClient, owner_engine: Engine
) -> None:
    data = SEED_PDF.read_bytes()
    response = _upload(client, data, filename="INV-SEED-001.pdf")
    assert response.status_code == 202
    body = response.json()
    sha = hashlib.sha256(data).hexdigest()
    assert body["status"] == "received"
    assert body["sha256"] == sha

    with owner_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT pdf, sha256, filename, size_bytes, page_count, status "
                "FROM invoices WHERE id = :id"
            ),
            {"id": body["id"]},
        ).one()
        job = conn.execute(
            text("SELECT stage, status FROM jobs WHERE invoice_id = :id"), {"id": body["id"]}
        ).one()
        event = conn.execute(
            text("SELECT actor, entity_type, entity_id, payload FROM audit_events")
        ).one()
        assert verify_chain(conn).ok is True
    assert bytes(row.pdf) == data
    assert (row.sha256, row.filename, row.size_bytes, row.page_count, row.status) == (
        sha,
        "INV-SEED-001.pdf",
        len(data),
        1,
        "received",
    )
    assert (job.stage, job.status) == ("extract", "queued")
    assert (event.actor, event.entity_type, event.entity_id) == ("api", "invoice", body["id"])
    assert event.payload == {
        "sha256": sha,
        "size": len(data),
        "page_count": 1,
        "filename": "INV-SEED-001.pdf",
    }


def test_same_file_twice_returns_the_same_invoice(client: TestClient, owner_engine: Engine) -> None:
    data = SEED_PDF.read_bytes()
    first = _upload(client, data)
    second = _upload(client, data, filename="renamed.pdf")
    assert (first.status_code, second.status_code) == (202, 200)
    assert second.json() == first.json()
    assert _counts(owner_engine) == {"invoices": 1, "jobs": 1, "received": 1}
    with owner_engine.connect() as conn:
        assert verify_chain(conn).ok is True


def test_filename_keeps_only_the_base_name(client: TestClient) -> None:
    response = _upload(client, text_pdf(), filename="../../etc/C:\\x\\inv.pdf")
    assert client.get(f"/invoices/{response.json()['id']}").json()["filename"] == "inv.pdf"


def test_lost_insert_race_returns_the_winner(app_engine: Engine, owner_engine: Engine) -> None:
    """Both requests missed the pre-check; the second INSERT hits the unique sha256."""
    data = text_pdf()
    sha = hashlib.sha256(data).hexdigest()
    with app_engine.connect() as conn:
        winner, created = store.store_invoice(conn, data, sha, "a.pdf", 1)
    with app_engine.connect() as conn:
        loser, created_again = store.store_invoice(conn, data, sha, "b.pdf", 1)
    assert (created, created_again) == (True, False)
    assert loser == winner
    assert _counts(owner_engine) == {"invoices": 1, "jobs": 1, "received": 1}


def test_concurrent_identical_uploads_create_one_invoice(
    app_engine: Engine, owner_engine: Engine
) -> None:
    data = text_pdf()
    sha = hashlib.sha256(data).hexdigest()
    threads_n = 6
    barrier = threading.Barrier(threads_n)
    results: list[tuple[store.InvoiceRecord, bool]] = []
    errors: list[BaseException] = []

    def upload() -> None:
        try:
            with app_engine.connect() as conn:
                barrier.wait()
                results.append(store.store_invoice(conn, data, sha, "c.pdf", 1))
        except BaseException as exc:  # surfaced in the main thread below
            errors.append(exc)

    threads = [threading.Thread(target=upload) for _ in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sum(created for _, created in results) == 1
    assert len({record.id for record, _ in results}) == 1
    assert _counts(owner_engine) == {"invoices": 1, "jobs": 1, "received": 1}
    with owner_engine.connect() as conn:
        assert verify_chain(conn).ok is True


def test_pdf_parsing_runs_off_the_event_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    on_loop: list[bool] = []

    def spy(data: bytes, max_pages: int) -> int:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return inspect_pdf(data, max_pages)

    monkeypatch.setattr(invoices_api, "inspect_pdf", spy)
    assert _upload(client, text_pdf()).status_code == 202
    assert on_loop == [False]


# --- GET ---------------------------------------------------------------------------------


def test_get_invoice_returns_its_summary(client: TestClient) -> None:
    created = _upload(client, SEED_PDF.read_bytes(), filename="INV-SEED-001.pdf").json()
    response = client.get(f"/invoices/{created['id']}")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"id", "status", "sha256", "filename", "page_count", "created_at"}
    assert body["id"] == created["id"]
    assert body["sha256"] == created["sha256"]
    assert (body["status"], body["filename"], body["page_count"]) == (
        "received",
        "INV-SEED-001.pdf",
        1,
    )
    assert body["created_at"]


def test_get_unknown_invoice_is_404(client: TestClient) -> None:
    _assert_error(client.get("/invoices/00000000-0000-0000-0000-000000000000"), 404, "not_found")


def test_get_with_an_invalid_uuid_is_422(client: TestClient) -> None:
    assert client.get("/invoices/not-a-uuid").status_code == 422


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("inv.pdf", "inv.pdf"),
        ("a/b/inv.pdf", "inv.pdf"),
        ("C:\\Users\\x\\inv.pdf", "inv.pdf"),
        ("dir/", "upload.pdf"),
        ("   ", "upload.pdf"),
        (None, "upload.pdf"),
        ("x" * 300 + ".pdf", "x" * 255),
    ],
)
def test_base_name(raw: str | None, stored: str) -> None:
    assert invoices_api._base_name(raw) == stored
