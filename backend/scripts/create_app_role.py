"""Create the restricted app role with plain SQL (never from the Neon console).

Roles created in the Neon console get `neon_superuser`, whose pg_write_all_data
bypasses REVOKE. A role created with CREATE ROLE does not.

Usage (owner connection, password from the environment, nothing printed):
    OWNER_DATABASE_URL=... APP_ROLE_PASSWORD=... uv run python scripts/create_app_role.py
"""

import os
import sys

import psycopg
from psycopg import sql

ROLE = "app"
PROBE_TABLE = "skeleton_probe"


def build_statements(password: str) -> list[sql.Composed]:
    role = sql.Identifier(ROLE)
    table = sql.Identifier(PROBE_TABLE)
    return [
        sql.SQL(
            "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = {name}) "
            "THEN CREATE ROLE {role} LOGIN NOINHERIT; END IF; END $$"
        ).format(name=sql.Literal(ROLE), role=role),
        sql.SQL("ALTER ROLE {role} WITH LOGIN PASSWORD {pw}").format(
            role=role, pw=sql.Literal(password)
        ),
        sql.SQL("GRANT USAGE ON SCHEMA public TO {role}").format(role=role),
        sql.SQL("REVOKE ALL ON {table} FROM {role}").format(table=table, role=role),
        sql.SQL("GRANT SELECT, INSERT ON {table} TO {role}").format(table=table, role=role),
        sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}").format(
            role=role
        ),
    ]


def main() -> int:
    url = os.environ.get("OWNER_DATABASE_URL")
    password = os.environ.get("APP_ROLE_PASSWORD")
    if not url or not password:
        print("OWNER_DATABASE_URL and APP_ROLE_PASSWORD are required", file=sys.stderr)
        return 2
    with psycopg.connect(url, autocommit=True) as conn:
        for statement in build_statements(password):
            conn.execute(statement)
    print(f"role '{ROLE}' ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
