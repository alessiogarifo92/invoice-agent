"""Create the restricted app role with plain SQL (never from the Neon console).

Roles created in the Neon console get `neon_superuser`, whose pg_write_all_data
bypasses REVOKE. A role created with CREATE ROLE does not.

Run it after `alembic upgrade head`: it grants on the tables the migrations created.

Usage (owner connection, password from the environment, nothing printed):
    OWNER_DATABASE_URL=... APP_ROLE_PASSWORD=... uv run python scripts/create_app_role.py
"""

import os
import sys

import psycopg
from psycopg import sql

ROLE = "app"
PROBE_TABLE = "skeleton_probe"
AUDIT_TABLE = "audit_events"
MASTER_TABLES = (
    "vendors",
    "vendor_bank_accounts",
    "purchase_orders",
    "po_lines",
    "contracts",
    "contract_prices",
    "contract_clauses",
    "vat_rates",
    "settings_kv",
)
PROCESSING_TABLES = (
    "invoices",
    "processing_runs",
    "extractions",
    "flags",
    "proposals",
    "approvals",
    "llm_calls",
    "jobs",
    "budget_state",
)

# The complete privilege table. The app never deletes anything, and audit_events is
# append-only: INSERT/SELECT, never UPDATE, DELETE or TRUNCATE.
GRANTS: dict[str, tuple[str, ...]] = {
    **{table: ("SELECT",) for table in MASTER_TABLES},
    **{table: ("SELECT", "INSERT", "UPDATE") for table in PROCESSING_TABLES},
    AUDIT_TABLE: ("SELECT", "INSERT"),
    PROBE_TABLE: ("SELECT", "INSERT"),
}


def libpq_url(url: str) -> str:
    # The env may carry SQLAlchemy's URL form; libpq does not know the driver suffix.
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def build_statements(password: str, role: str = ROLE) -> list[sql.Composed]:
    ident = sql.Identifier(role)
    statements = [
        sql.SQL(
            "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = {name}) "
            "THEN CREATE ROLE {role} LOGIN NOINHERIT; END IF; END $$"
        ).format(name=sql.Literal(role), role=ident),
        sql.SQL("ALTER ROLE {role} WITH LOGIN NOINHERIT PASSWORD {pw}").format(
            role=ident, pw=sql.Literal(password)
        ),
        sql.SQL("GRANT USAGE ON SCHEMA public TO {role}").format(role=ident),
        # Start from nothing, so a privilege removed here is also removed in the database.
        sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}").format(role=ident),
    ]
    for table, privileges in GRANTS.items():
        statements.append(
            sql.SQL("GRANT {privs} ON {table} TO {role}").format(
                privs=sql.SQL(", ").join(sql.SQL(p) for p in privileges),
                table=sql.Identifier(table),
                role=ident,
            )
        )
    statements += [
        # Explicit, even after REVOKE ALL: the audit chain must never be rewritten by the app.
        sql.SQL("REVOKE UPDATE, DELETE, TRUNCATE ON {table} FROM {role}").format(
            table=sql.Identifier(AUDIT_TABLE), role=ident
        ),
        sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}").format(
            role=ident
        ),
    ]
    return statements


def main() -> int:
    url = os.environ.get("OWNER_DATABASE_URL")
    password = os.environ.get("APP_ROLE_PASSWORD")
    if not url or not password:
        print("OWNER_DATABASE_URL and APP_ROLE_PASSWORD are required", file=sys.stderr)
        return 2
    with psycopg.connect(libpq_url(url), autocommit=True) as conn:
        for statement in build_statements(password):
            conn.execute(statement)
    print(f"role '{ROLE}' ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
