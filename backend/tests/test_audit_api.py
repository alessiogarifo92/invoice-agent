"""GET /audit/verify against a scratch database, through the overridable DB dependency."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, text

from app.audit import GENESIS_HASH, append_event
from app.db import get_connection
from app.main import create_app


@pytest.fixture
def client(app_engine: Engine) -> Iterator[TestClient]:
    # The API runs as the restricted app role, like in production.
    def override() -> Iterator[Connection]:
        with app_engine.connect() as conn:
            yield conn

    app = create_app()
    app.dependency_overrides[get_connection] = override
    with TestClient(app) as test_client:
        yield test_client


def test_empty_chain_verifies(client: TestClient) -> None:
    response = client.get("/audit/verify")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "checked": 0,
        "first_broken_seq": None,
        "reason": None,
        "head_hash": None,
    }


def test_intact_chain_reports_its_head(client: TestClient, owner_engine: Engine) -> None:
    with owner_engine.begin() as conn:
        append_event(conn, "api", "invoice.received", "invoice", "inv-1", {})
        head = append_event(conn, "api", "invoice.extracted", "invoice", "inv-1", {"ok": True})
    body = client.get("/audit/verify").json()
    assert body["ok"] is True
    assert body["checked"] == 2
    assert body["head_hash"] == head.hash
    assert body["head_hash"] != GENESIS_HASH


def test_tampered_row_is_reported(client: TestClient, owner_engine: Engine) -> None:
    with owner_engine.begin() as conn:
        events = [
            append_event(conn, "api", "invoice.received", "invoice", f"inv-{n}", {"n": n})
            for n in range(3)
        ]
    target = events[1].seq
    with owner_engine.begin() as conn:
        conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER USER"))
        conn.execute(
            text("""UPDATE audit_events SET payload = '{"n": "forged"}' WHERE seq = :seq"""),
            {"seq": target},
        )
        conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER USER"))

    body = client.get("/audit/verify").json()
    assert body["ok"] is False
    assert body["first_broken_seq"] == target
    assert body["reason"] == "hash_mismatch"
    assert body["checked"] == 3
