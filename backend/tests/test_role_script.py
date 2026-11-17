import re

import psycopg
import pytest

from tests.support import ScratchDatabase, load_script

PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def _sql(password: str = "s3cret'; DROP TABLE x; --", role: str = "app") -> list[str]:
    module = load_script("create_app_role")
    return [s.as_string() for s in module.build_statements(password, role=role)]


def _grants_on(table: str, statements: list[str]) -> list[str]:
    return [s for s in statements if s.startswith("GRANT ") and f' ON "{table}" ' in s]


def test_main_requires_both_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_script("create_app_role")
    monkeypatch.delenv("OWNER_DATABASE_URL", raising=False)
    monkeypatch.delenv("APP_ROLE_PASSWORD", raising=False)
    assert module.main() == 2


def test_libpq_url_drops_the_sqlalchemy_driver_name() -> None:
    module = load_script("create_app_role")
    assert module.libpq_url("postgresql+psycopg://o:p@h/db") == "postgresql://o:p@h/db"
    assert module.libpq_url("postgresql://o:p@h/db") == "postgresql://o:p@h/db"


def test_role_is_plain_noinherit_and_never_superuser() -> None:
    text = " ".join(_sql()).upper()
    assert 'CREATE ROLE "APP" LOGIN NOINHERIT' in text
    assert "SUPERUSER" not in text
    assert "BYPASSRLS" not in text


def test_password_is_passed_as_an_escaped_literal() -> None:
    assert "PASSWORD 's3cret''; DROP TABLE x; --'" in " ".join(_sql())


def test_audit_events_is_insert_select_only() -> None:
    module = load_script("create_app_role")
    assert module.GRANTS["audit_events"] == ("SELECT", "INSERT")
    statements = _sql()
    for grant in _grants_on("audit_events", statements):
        assert not re.search(r"\b(UPDATE|DELETE|TRUNCATE)\b", grant), grant
    assert 'REVOKE UPDATE, DELETE, TRUNCATE ON "audit_events" FROM "app"' in statements


def test_no_table_is_ever_granted_delete_or_truncate() -> None:
    module = load_script("create_app_role")
    for table, privileges in module.GRANTS.items():
        assert "DELETE" not in privileges, table
        assert "TRUNCATE" not in privileges, table
    for statement in _sql():
        if statement.startswith("GRANT "):
            assert "DELETE" not in statement and "TRUNCATE" not in statement, statement


def test_master_data_is_read_only_and_processing_is_writable() -> None:
    module = load_script("create_app_role")
    for table in module.MASTER_TABLES:
        assert module.GRANTS[table] == ("SELECT",)
    for table in module.PROCESSING_TABLES:
        assert module.GRANTS[table] == ("SELECT", "INSERT", "UPDATE")
    assert module.GRANTS["skeleton_probe"] == ("SELECT", "INSERT")


def test_role_name_is_quoted_as_an_identifier() -> None:
    assert all('"ia_test_x"' in s for s in _sql(role="ia_test_x") if " ROLE " in s or " TO " in s)


def test_db_privileges_match_the_grant_table(test_db: ScratchDatabase) -> None:
    module = load_script("create_app_role")
    role = test_db.app_url.split("://", 1)[1].split(":", 1)[0]
    with psycopg.connect(test_db.url) as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename <> 'alembic_version'"
            ).fetchall()
        }
        # Every table in the schema has an explicit entry: a new table cannot slip through.
        assert tables == set(module.GRANTS)
        for table, expected in module.GRANTS.items():
            held = {
                p
                for p in PRIVILEGES
                if conn.execute(
                    "SELECT has_table_privilege(%s, %s, %s)", (role, f"public.{table}", p)
                ).fetchone()
                == (True,)
            }
            assert held == set(expected), table
        assert conn.execute(
            "SELECT rolsuper, rolinherit FROM pg_roles WHERE rolname = %s", (role,)
        ).fetchone() == (False, False)
