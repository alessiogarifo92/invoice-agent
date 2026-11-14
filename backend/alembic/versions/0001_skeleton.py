"""Skeleton: pg_trgm and a probe table.

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Trigram matching is used by vendor/invoice-number lookups (phase 3, B1 retrieval).
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    # Probe table for the walking skeleton: the restricted app role must be able to
    # INSERT/SELECT but not UPDATE/DELETE it. The real audit table lands in T2.
    op.execute(
        """
        CREATE TABLE skeleton_probe (
            id bigserial PRIMARY KEY,
            note text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS skeleton_probe")
