import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "create_app_role.py"


def _load() -> object:
    spec = importlib.util.spec_from_file_location("create_app_role", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_main_requires_both_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    monkeypatch.delenv("OWNER_DATABASE_URL", raising=False)
    monkeypatch.delenv("APP_ROLE_PASSWORD", raising=False)
    assert module.main() == 2  # type: ignore[attr-defined]


def test_app_role_is_never_granted_update_or_delete() -> None:
    module = _load()
    statements = module.build_statements("s3cret'; DROP TABLE x; --")  # type: ignore[attr-defined]
    # repr() of a psycopg Composed shows its SQL fragments without needing a connection.
    text = " ".join(repr(s) for s in statements).upper()
    assert "GRANT SELECT, INSERT ON" in text
    assert "UPDATE" not in text
    assert "DELETE" not in text
    assert "SUPERUSER" not in text
