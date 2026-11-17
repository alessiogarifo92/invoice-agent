"""The Postgres job queue on a real database, as the restricted app role.

Claims use FOR UPDATE SKIP LOCKED, leases carry a heartbeat, and only expired leases are
requeued by the sweep (phase 3, "Background processing: A2").
"""

import threading
import time
from collections import Counter
from uuid import UUID

import pytest
from sqlalchemy import Engine, text

from app.jobs import queue
from tests.jobs_support import insert_invoice, rows, scalar

LEASE_S = 60.0


def _queued(engine: Engine, name: str) -> tuple[UUID, UUID]:
    invoice_id = insert_invoice(engine, name)
    with engine.begin() as conn:
        job_id = queue.enqueue(conn, invoice_id)
    assert job_id is not None
    return invoice_id, job_id


def _claim(engine: Engine, owner: str, lease_s: float = LEASE_S) -> queue.Job | None:
    with engine.begin() as conn:
        return queue.claim(conn, owner, lease_s)


def _expire(owner_engine: Engine, job_id: UUID) -> None:
    with owner_engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"),
            {"id": job_id},
        )


# --- enqueue -----------------------------------------------------------------------------


def test_a_second_enqueue_of_a_live_job_is_rejected(app_engine: Engine) -> None:
    invoice_id, job_id = _queued(app_engine, "a")
    with app_engine.begin() as conn:
        assert queue.enqueue(conn, invoice_id) is None
    assert scalar(app_engine, "SELECT count(*) FROM jobs") == 1
    # Running is live too.
    assert _claim(app_engine, "w1") is not None
    with app_engine.begin() as conn:
        assert queue.enqueue(conn, invoice_id) is None


def test_a_finished_invoice_can_be_enqueued_again(app_engine: Engine) -> None:
    invoice_id, job_id = _queued(app_engine, "a")
    with app_engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET status = 'failed' WHERE id = :id"), {"id": job_id})
        second = queue.enqueue(conn, invoice_id)
    assert second is not None and second != job_id


def test_concurrent_enqueues_create_one_job(app_engine: Engine) -> None:
    invoice_id = insert_invoice(app_engine, "a")
    barrier = threading.Barrier(4)
    results: list[UUID | None] = []
    lock = threading.Lock()

    def enqueue() -> None:
        with app_engine.begin() as conn:
            barrier.wait()
            job_id = queue.enqueue(conn, invoice_id)
        with lock:
            results.append(job_id)

    threads = [threading.Thread(target=enqueue) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r is not None for r in results) == 1
    assert scalar(app_engine, "SELECT count(*) FROM jobs") == 1


# --- claim -------------------------------------------------------------------------------


def test_claim_takes_the_oldest_job_leases_it_and_marks_the_invoice(app_engine: Engine) -> None:
    first_invoice, first = _queued(app_engine, "a")
    _queued(app_engine, "b")
    job = _claim(app_engine, "w1")
    assert job is not None
    assert (job.id, job.invoice_id, job.status, job.stage) == (
        first,
        first_invoice,
        "running",
        "extract",
    )
    assert job.attempts == 1 and job.lease_owner == "w1"
    (row,) = rows(
        app_engine,
        "SELECT lease_expires_at - heartbeat_at AS lease, heartbeat_at FROM jobs WHERE id = :id",
        id=first,
    )
    assert row.lease.total_seconds() == pytest.approx(LEASE_S)
    assert scalar(app_engine, "SELECT status FROM invoices WHERE id = :id", id=first_invoice) == (
        "processing"
    )


def test_claim_returns_none_on_an_empty_queue(app_engine: Engine) -> None:
    assert _claim(app_engine, "w1") is None


def test_claim_skips_a_job_scheduled_for_later(app_engine: Engine) -> None:
    _, job_id = _queued(app_engine, "a")
    with app_engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET run_after = now() + interval '1 hour' WHERE id = :id"),
            {"id": job_id},
        )
    assert _claim(app_engine, "w1") is None
    with app_engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET run_after = now() - interval '1 second' WHERE id = :id"),
            {"id": job_id},
        )
    claimed = _claim(app_engine, "w1")
    assert claimed is not None and claimed.id == job_id


def test_a_claim_skips_a_row_another_worker_holds_without_waiting(app_engine: Engine) -> None:
    _queued(app_engine, "a")
    with app_engine.connect() as a, app_engine.connect() as b:
        a.begin()
        assert queue.claim(a, "worker-a", LEASE_S) is not None
        b.begin()
        # Without SKIP LOCKED this claim would wait for A's lock; the timeout turns a wait
        # into an error instead of a hang.
        b.execute(text("SET LOCAL lock_timeout = '2s'"))
        assert queue.claim(b, "worker-b", LEASE_S) is None
        b.rollback()
        a.commit()
        b.begin()
        assert queue.claim(b, "worker-b", LEASE_S) is None
        b.rollback()
    (owner,) = rows(app_engine, "SELECT lease_owner FROM jobs")
    assert owner.lease_owner == "worker-a"


def test_two_workers_racing_for_one_job_only_one_wins(app_engine: Engine) -> None:
    _queued(app_engine, "a")
    barrier = threading.Barrier(2)
    won: dict[str, bool] = {}

    def worker(name: str) -> None:
        with app_engine.connect() as conn, conn.begin():
            barrier.wait()
            won[name] = queue.claim(conn, name, LEASE_S) is not None
            time.sleep(0.2)  # hold the row lock so the two claims overlap

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("w1", "w2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(won.values()) == [False, True]
    assert scalar(app_engine, "SELECT attempts FROM jobs") == 1


def test_many_jobs_and_workers_claim_each_job_exactly_once(app_engine: Engine) -> None:
    job_ids = {_queued(app_engine, f"inv-{n}")[1] for n in range(20)}
    claimed: list[UUID] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        while True:
            job = _claim(app_engine, name)
            if job is None:
                return
            with lock:
                claimed.append(job.id)

    threads = [threading.Thread(target=worker, args=(f"w{n}",)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert Counter(claimed) == Counter(job_ids)


# --- leases ------------------------------------------------------------------------------


def test_the_heartbeat_extends_only_the_owners_lease(app_engine: Engine) -> None:
    _, job_id = _queued(app_engine, "a")
    assert _claim(app_engine, "w1", lease_s=5) is not None
    before = scalar(app_engine, "SELECT lease_expires_at FROM jobs WHERE id = :id", id=job_id)
    with app_engine.begin() as conn:
        assert queue.heartbeat(conn, job_id, "w1", LEASE_S) is True
    after = scalar(app_engine, "SELECT lease_expires_at FROM jobs WHERE id = :id", id=job_id)
    assert after > before
    with app_engine.begin() as conn:
        assert queue.heartbeat(conn, job_id, "intruder", LEASE_S) is False
    assert scalar(app_engine, "SELECT lease_expires_at FROM jobs WHERE id = :id", id=job_id) == (
        after
    )


def test_an_expired_lease_is_requeued_and_a_live_one_is_not(
    app_engine: Engine, owner_engine: Engine
) -> None:
    expired_invoice, expired = _queued(app_engine, "a")
    _, live = _queued(app_engine, "b")
    assert _claim(app_engine, "old") is not None
    assert _claim(app_engine, "old") is not None
    _expire(owner_engine, expired)

    with app_engine.begin() as conn:
        sweep = queue.sweep_expired(conn, max_attempts=3)
    assert [j.id for j in sweep.requeued] == [expired]
    assert sweep.failed == ()
    states = {
        r.id: (r.status, r.lease_owner, r.lease_expires_at is None)
        for r in rows(app_engine, "SELECT id, status, lease_owner, lease_expires_at FROM jobs")
    }
    assert states[expired] == ("queued", None, True)
    assert states[live] == ("running", "old", False)
    assert "job.lease_expired" in [
        r.event_type
        for r in rows(
            app_engine,
            "SELECT event_type FROM audit_events WHERE entity_id = :id",
            id=str(expired_invoice),
        )
    ]
    # The old owner has lost it: no heartbeat, no stage commit.
    with app_engine.begin() as conn:
        assert queue.heartbeat(conn, expired, "old", LEASE_S) is False
        with pytest.raises(queue.LeaseLost):
            queue.lock_owned(conn, expired, "old")
    # Another worker picks it up.
    again = _claim(app_engine, "new")
    assert again is not None and again.id == expired and again.attempts == 2


def test_an_expired_lease_with_no_attempts_left_fails_the_job(
    app_engine: Engine, owner_engine: Engine
) -> None:
    invoice_id, job_id = _queued(app_engine, "a")
    assert _claim(app_engine, "old") is not None
    _expire(owner_engine, job_id)
    with app_engine.begin() as conn:
        sweep = queue.sweep_expired(conn, max_attempts=1)
    assert sweep.requeued == () and [j.id for j in sweep.failed] == [job_id]
    (job,) = rows(app_engine, "SELECT status, last_error FROM jobs WHERE id = :id", id=job_id)
    assert job.status == "failed" and "lease expired" in job.last_error
    assert scalar(app_engine, "SELECT status FROM invoices WHERE id = :id", id=invoice_id) == (
        "failed"
    )


def test_the_sweep_leaves_queued_and_finished_jobs_alone(app_engine: Engine) -> None:
    _queued(app_engine, "a")
    with app_engine.begin() as conn:
        sweep = queue.sweep_expired(conn, max_attempts=3)
    assert sweep.requeued == () and sweep.failed == ()


def test_lock_owned_returns_the_job_for_its_owner(app_engine: Engine) -> None:
    _, job_id = _queued(app_engine, "a")
    assert _claim(app_engine, "w1") is not None
    with app_engine.begin() as conn:
        assert queue.lock_owned(conn, job_id, "w1").id == job_id
        with pytest.raises(queue.LeaseLost):
            queue.lock_owned(conn, job_id, "w2")


# --- reads -------------------------------------------------------------------------------


def test_latest_job_is_the_newest_for_the_invoice(app_engine: Engine) -> None:
    invoice_id, first = _queued(app_engine, "a")
    with app_engine.connect() as conn:
        latest = queue.latest_job(conn, invoice_id)
    assert latest is not None and latest.id == first and latest.status == "queued"
    with app_engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET status = 'failed' WHERE id = :id"), {"id": first})
        second = queue.enqueue(conn, invoice_id)
    with app_engine.connect() as conn:
        latest = queue.latest_job(conn, invoice_id)
    assert latest is not None and latest.id == second


def test_latest_job_is_none_without_jobs(app_engine: Engine) -> None:
    invoice_id = insert_invoice(app_engine, "a")
    with app_engine.connect() as conn:
        assert queue.latest_job(conn, invoice_id) is None
