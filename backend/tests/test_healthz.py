from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app


def test_healthz_returns_ok_without_database() -> None:
    # No database is configured or running in this test: /healthz must still answer.
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_unknown_route_is_404() -> None:
    client = TestClient(app)
    assert client.get("/nope").status_code == 404


def test_cors_origin_list_ignores_blanks() -> None:
    settings = Settings(cors_origins=" https://a.example , ,http://localhost:5173 ")
    assert settings.cors_origin_list == ["https://a.example", "http://localhost:5173"]
