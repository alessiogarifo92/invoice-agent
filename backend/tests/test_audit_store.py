"""audit_events on a real Postgres: append, verify, concurrency, permissions, trigger."""

import threading
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app.audit import GENESIS_HASH, append_event, compute_hash, verify_chain

THREADS = 8
EVENTS_PER_THREAD = 25


def _append(engine: Engine, payload: dict[str, Any], entity_id: str = "inv-1") -> None:
    with engine.begin() as conn:
        append_event(conn, "api", "invoice.received", "invoice", entity_id, payload)


def test_first_event_links_to_genesis_and_the_next_to_it(owner_engine: Engine) -> None:
    with owner_engine.begin() as conn:
        first = append_event(conn, "api", "invoice.received", "invoice", "inv-1", {"a": 1})
        second = append_event(conn, "worker", "invoice.extracted", "invoice", "inv-1", {})
    assert first.prev_hash == GENESIS_HASH
    assert second.prev_hash == first.hash
    assert second.seq > first.seq
    assert first.hash == compute_hash(
        prev_hash=GENESIS_HASH,
        event_type="invoice.received",
        entity_type="invoice",
        entity_id="inv-1",
        actor="api",
        payload={"a": 1},
        occurred_at=first.occurred_at,
    )


def test_payload_is_hashed_as_postgres_stores_it(owner_engine: Engine) -> None:
    # jsonb rewrites 1e20 as 100000000000000000000; the hash must survive the round trip.
    _append(owner_engine, {"big": 1e20, "ratio": 0.1, "nested": {"z": [1, "x"], "a": None}})
    with owner_engine.connect() as conn:
        assert verify_chain(conn).ok is True


def test_rolled_back_event_leaves_the_chain_intact(owner_engine: Engine) -> None:
    _append(owner_engine, {"n": 1})
    with owner_engine.connect() as conn:
        append_event(conn, "api", "invoice.received", "invoice", "inv-2", {"n": 2})
        conn.rollback()
    _append(owner_engine, {"n": 3})
    with owner_engine.connect() as conn:
        result = verify_chain(conn)
    assert result.ok is True
    assert result.checked == 2


def test_concurrent_writers_never_fork_the_chain(app_engine: Engine) -> None:
    barrier = threading.Barrier(THREADS)
    errors: list[BaseException] = []

    def writer(worker: int) -> None:
        try:
            with app_engine.connect() as conn:
                barrier.wait()
                for i in range(EVENTS_PER_THREAD):
                    with conn.begin():
                        append_event(
                            conn, f"worker-{worker}", "job.stage", "job", str(worker), {"i": i}
                        )
        except BaseException as exc:  # surfaced in the main thread below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(w,)) for w in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []

    with app_engine.connect() as conn:
        result = verify_chain(conn)
        rows, distinct_prev = conn.execute(
            text("SELECT count(*), count(DISTINCT prev_hash) FROM audit_events")
        ).one()
    assert result.ok is True, result
    assert result.checked == THREADS * EVENTS_PER_THREAD
    assert rows == THREADS * EVENTS_PER_THREAD
    assert distinct_prev == rows


def test_app_role_can_insert_and_select(app_engine: Engine) -> None:
    _append(app_engine, {"via": "app role"})
    with app_engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM audit_events")).scalar_one()
        assert count == 1
        assert verify_chain(conn).ok is True


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_events SET actor = 'mallory'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
    ],
)
def test_app_role_cannot_rewrite_history(app_engine: Engine, statement: str) -> None:
    _append(app_engine, {"n": 1})
    with app_engine.connect() as conn, pytest.raises(DBAPIError, match="permission denied"):
        conn.execute(text(statement))


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_events SET actor = 'mallory'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
    ],
)
def test_trigger_blocks_even_the_owner(owner_engine: Engine, statement: str) -> None:
    _append(owner_engine, {"n": 1})
    with owner_engine.connect() as conn, pytest.raises(DBAPIError, match="append-only"):
        conn.execute(text(statement))


def test_verify_chain_finds_a_row_edited_behind_the_trigger(owner_engine: Engine) -> None:
    for n in range(4):
        _append(owner_engine, {"n": n})
    with owner_engine.begin() as conn:
        target = conn.execute(text("SELECT seq FROM audit_events ORDER BY seq OFFSET 2")).scalar()
        conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER USER"))
        conn.execute(
            text("""UPDATE audit_events SET payload = '{"n": 99}' WHERE seq = :seq"""),
            {"seq": target},
        )
        conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER USER"))
    with owner_engine.connect() as conn:
        result = verify_chain(conn)
    assert result.ok is False
    assert result.first_broken_seq == target
    assert result.reason == "hash_mismatch"
