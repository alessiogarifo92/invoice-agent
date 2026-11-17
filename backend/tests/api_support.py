"""Helpers for the API tests: an app wired to a scratch database, and a test-only demo token.

The token is a fixed test string, never a real secret; the app only ever sees its hash.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine

from app.api.auth import token_hash
from app.db import get_connection
from app.main import create_app
from tests.jobs_support import make_settings

TOKEN = "test-demo-token-not-a-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@contextmanager
def api_client(
    engine: Engine, headers: dict[str, str] | None = None, **settings: Any
) -> Iterator[TestClient]:
    """A TestClient whose requests run on `engine` (the restricted app role in the tests),
    with TOKEN accepted as the demo token unless `demo_token_hashes` is overridden."""
    settings.setdefault("demo_token_hashes", token_hash(TOKEN))
    app = create_app(make_settings(**settings))

    def override() -> Iterator[Connection]:
        with engine.connect() as conn:
            yield conn

    app.dependency_overrides[get_connection] = override
    with TestClient(app, headers=headers) as client:
        yield client
