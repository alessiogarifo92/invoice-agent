"""The Postgres job queue (phase 3, "Background processing: A2").

- `claim` takes the oldest due job with FOR UPDATE SKIP LOCKED, so concurrent workers never
  wait on each other and never share a job; it leases the job to one owner.
- A running job's lease is kept alive by `heartbeat`. Only a lease that has expired is
  handed back by `sweep_expired`, so an old instance still working during an overlapping
  deploy keeps its job.
- Every stage commit first takes `lock_owned`: a worker that lost its lease raises LeaseLost
  and writes nothing more.
- The partial unique index jobs_one_active_per_invoice (0002) makes `enqueue` a no-op while
  the invoice already has a queued or running job.

Every function runs in the caller's transaction, except `latest_job`, a read for the API.
Times come from the database clock (now()), the one clock every instance shares.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Row, text

from app.audit import append_event

ACTOR = "worker"
ERROR_MAX = 200

_COLUMNS = (
    "id, invoice_id, stage, status, attempts, lease_owner, lease_expires_at, run_after, "
    "stage_outputs, last_error"
)
_J_COLUMNS = ", ".join(f"j.{column.strip()}" for column in _COLUMNS.split(","))
_SECONDS = "CAST(:{name} AS double precision) * interval '1 second'"


class LeaseLost(Exception):
    """This worker no longer owns the job: another one took it after its lease expired."""

    def __init__(self, job_id: UUID):
        super().__init__(f"job {job_id}: lease lost")
        self.job_id = job_id


@dataclass(frozen=True)
class Job:
    id: UUID
    invoice_id: UUID
    stage: str
    status: str
    attempts: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    run_after: datetime
    stage_outputs: dict[str, Any]
    last_error: str | None


@dataclass(frozen=True)
class Sweep:
    requeued: tuple[Job, ...] = ()
    failed: tuple[Job, ...] = ()


def _job(row: Row[Any]) -> Job:
    return Job(
        id=row.id,
        invoice_id=row.invoice_id,
        stage=row.stage,
        status=row.status,
        attempts=row.attempts,
        lease_owner=row.lease_owner,
        lease_expires_at=row.lease_expires_at,
        run_after=row.run_after,
        stage_outputs=dict(row.stage_outputs),
        last_error=row.last_error,
    )


def error_text(exc: BaseException) -> str:
    """What jobs.last_error keeps: the class and the message, bounded."""
    return f"{type(exc).__name__}: {exc}"[:ERROR_MAX]


def set_invoice_status(conn: Connection, invoice_id: UUID, status: str) -> None:
    """Move the invoice to `status`, bumping `version` (the If-Match token) on a change."""
    conn.execute(
        text(
            "UPDATE invoices SET status = :status, version = version + 1, updated_at = now() "
            "WHERE id = :id AND status <> :status"
        ),
        {"id": invoice_id, "status": status},
    )


def enqueue(conn: Connection, invoice_id: UUID, stage: str = "extract") -> UUID | None:
    """A new queued job, or None when the invoice already has a live (queued/running) one."""
    job_id: UUID | None = conn.execute(
        text(
            "INSERT INTO jobs (invoice_id, stage, status) VALUES (:id, :stage, 'queued') "
            "ON CONFLICT (invoice_id) WHERE status IN ('queued', 'running') DO NOTHING "
            "RETURNING id"
        ),
        {"id": invoice_id, "stage": stage},
    ).scalar_one_or_none()
    return job_id


def claim(conn: Connection, owner: str, lease_s: float) -> Job | None:
    """Lease the oldest due queued job to `owner`, or None when nothing is due.

    Counts the attempt and marks the invoice `processing`, in the caller's transaction.
    """
    row = conn.execute(
        text(
            "WITH next AS (SELECT id FROM jobs WHERE status = 'queued' AND run_after <= now() "
            "ORDER BY run_after, created_at LIMIT 1 FOR UPDATE SKIP LOCKED) "
            "UPDATE jobs j SET status = 'running', lease_owner = :owner, "
            "attempts = j.attempts + 1, heartbeat_at = now(), "
            f"lease_expires_at = now() + {_SECONDS.format(name='lease')}, updated_at = now() "
            f"FROM next WHERE j.id = next.id RETURNING {_J_COLUMNS}"
        ),
        {"owner": owner, "lease": lease_s},
    ).one_or_none()
    if row is None:
        return None
    job = _job(row)
    set_invoice_status(conn, job.invoice_id, "processing")
    return job


def heartbeat(conn: Connection, job_id: UUID, owner: str, lease_s: float) -> bool:
    """Extend the lease from now. False when `owner` no longer holds it."""
    result = conn.execute(
        text(
            "UPDATE jobs SET heartbeat_at = now(), "
            f"lease_expires_at = now() + {_SECONDS.format(name='lease')}, updated_at = now() "
            "WHERE id = :id AND status = 'running' AND lease_owner = :owner"
        ),
        {"id": job_id, "owner": owner, "lease": lease_s},
    )
    return result.rowcount == 1


def lock_owned(conn: Connection, job_id: UUID, owner: str) -> Job:
    """Lock the job row for a stage commit; LeaseLost unless `owner` still runs it."""
    row = conn.execute(
        text(
            f"SELECT {_COLUMNS} FROM jobs WHERE id = :id AND status = 'running' "
            "AND lease_owner = :owner FOR UPDATE"
        ),
        {"id": job_id, "owner": owner},
    ).one_or_none()
    if row is None:
        raise LeaseLost(job_id)
    return _job(row)


def advance(conn: Connection, job_id: UUID, stage: str, outputs: dict[str, Any]) -> None:
    """Record the completed stages' outputs and the stage to run next."""
    conn.execute(
        text(
            "UPDATE jobs SET stage = :stage, stage_outputs = CAST(:outputs AS jsonb), "
            "updated_at = now() WHERE id = :id"
        ),
        {"id": job_id, "stage": stage, "outputs": json.dumps(outputs)},
    )


def complete(conn: Connection, job_id: UUID, outputs: dict[str, Any]) -> None:
    conn.execute(
        text(
            "UPDATE jobs SET status = 'done', stage_outputs = CAST(:outputs AS jsonb), "
            "lease_owner = NULL, lease_expires_at = NULL, updated_at = now() WHERE id = :id"
        ),
        {"id": job_id, "outputs": json.dumps(outputs)},
    )


def requeue(
    conn: Connection,
    job_id: UUID,
    *,
    delay_s: float = 0.0,
    run_after: datetime | None = None,
    error: str | None = None,
    refund_attempt: bool = False,
) -> datetime:
    """Back to queued, unleased, due at `run_after` (else now + delay_s). Returns the due time.

    `refund_attempt` un-counts the claim's attempt: waiting for quota or a shutdown is not a
    failure, so it must not use up the job's retries.
    """
    due: datetime = conn.execute(
        text(
            "UPDATE jobs SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL, "
            "run_after = COALESCE(CAST(:run_after AS timestamptz), "
            f"now() + {_SECONDS.format(name='delay')}), "
            "attempts = attempts - CAST(:refund AS integer), "
            "last_error = COALESCE(:error, last_error), updated_at = now() "
            "WHERE id = :id RETURNING run_after"
        ),
        {
            "id": job_id,
            "run_after": run_after,
            "delay": delay_s,
            "refund": int(refund_attempt),
            "error": error,
        },
    ).scalar_one()
    return due


def fail(conn: Connection, job_id: UUID, error: str) -> None:
    conn.execute(
        text(
            "UPDATE jobs SET status = 'failed', lease_owner = NULL, lease_expires_at = NULL, "
            "last_error = :error, updated_at = now() WHERE id = :id"
        ),
        {"id": job_id, "error": error[:ERROR_MAX]},
    )


def fail_job_and_invoice(conn: Connection, job: Job, error: str, error_class: str) -> None:
    """The terminal path: job `failed`, invoice `failed`, one `job.failed` audit event."""
    fail(conn, job.id, error)
    set_invoice_status(conn, job.invoice_id, "failed")
    append_event(
        conn,
        ACTOR,
        "job.failed",
        "invoice",
        str(job.invoice_id),
        {"job_id": str(job.id), "stage": job.stage, "attempts": job.attempts, "error": error_class},
    )


def sweep_expired(conn: Connection, max_attempts: int) -> Sweep:
    """Hand back every running job whose lease has expired; live leases are left alone.

    A job that has used up its attempts fails instead, so a job that kills its worker every
    time cannot loop forever. Rows another transaction holds are skipped, not waited for.
    """
    rows = conn.execute(
        text(
            f"SELECT {_COLUMNS} FROM jobs WHERE status = 'running' AND lease_expires_at < now() "
            "ORDER BY lease_expires_at FOR UPDATE SKIP LOCKED"
        )
    ).all()
    requeued: list[Job] = []
    failed: list[Job] = []
    for job in map(_job, rows):
        if job.attempts >= max_attempts:
            fail_job_and_invoice(
                conn, job, f"lease expired after {job.attempts} attempts", "LeaseExpired"
            )
            failed.append(job)
            continue
        requeue(conn, job.id)
        append_event(
            conn,
            ACTOR,
            "job.lease_expired",
            "invoice",
            str(job.invoice_id),
            {
                "job_id": str(job.id),
                "stage": job.stage,
                "previous_owner": job.lease_owner,
                "attempts": job.attempts,
            },
        )
        requeued.append(job)
    return Sweep(requeued=tuple(requeued), failed=tuple(failed))


def latest_job(conn: Connection, invoice_id: UUID) -> Job | None:
    """The invoice's newest job, read in its own short transaction (for GET /invoices/{id})."""
    with conn.begin():
        row = conn.execute(
            text(
                f"SELECT {_COLUMNS} FROM jobs WHERE invoice_id = :id "
                "ORDER BY created_at DESC, id DESC LIMIT 1"
            ),
            {"id": invoice_id},
        ).one_or_none()
    return None if row is None else _job(row)
