"""jobs.run_after: the earliest time a queued job may be claimed (T7).

A job parked for quota (awaiting_quota, phase 2 amendment) or waiting out a retry backoff
must stay queued but unclaimable until then; 0002 has no column that means this, and the
lease columns describe a running job. NOT NULL with a default of now(), so an ordinary
enqueue is claimable at once and the claim predicate stays a plain range on the index.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs ADD COLUMN run_after timestamptz NOT NULL DEFAULT now()")
    # The claim reads queued jobs due now, oldest first: replaces 0002's created_at index.
    op.execute("DROP INDEX jobs_queued_idx")
    op.execute(
        "CREATE INDEX jobs_queued_idx ON jobs (run_after, created_at) WHERE status = 'queued'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX jobs_queued_idx")
    op.execute("CREATE INDEX jobs_queued_idx ON jobs (created_at) WHERE status = 'queued'")
    op.execute("ALTER TABLE jobs DROP COLUMN run_after")
