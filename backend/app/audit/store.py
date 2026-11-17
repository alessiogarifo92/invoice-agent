"""audit_events reads and writes. Every append runs inside the caller's transaction."""

import json
from collections.abc import Iterator
from typing import Any

from sqlalchemy import Connection, text

from app.audit.chain import GENESIS_HASH, AuditEvent, VerifyResult, compute_hash, verify_events

# Fixed advisory-lock key ("auditchn" as a big-endian int64) that serialises chain appends.
AUDIT_CHAIN_LOCK_KEY = int.from_bytes(b"auditchn", "big")

_COLUMNS = "seq, occurred_at, actor, event_type, entity_type, entity_id, payload, prev_hash, hash"


def append_event(
    conn: Connection,
    actor: str,
    event_type: str,
    entity_type: str,
    entity_id: str,
    payload: dict[str, Any],
) -> AuditEvent:
    """Append one event to the chain. The caller commits (or rolls back) the transaction.

    The transaction-scoped advisory lock makes concurrent writers take turns, so each one
    reads the head only after the previous writer has committed. That relies on READ
    COMMITTED (the Postgres default): under REPEATABLE READ a stale head would be read, and
    the UNIQUE constraint on prev_hash would reject the insert rather than fork the chain.
    """
    conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": AUDIT_CHAIN_LOCK_KEY})
    prev_hash = conn.execute(
        text("SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1")
    ).scalar_one_or_none()
    if prev_hash is None:
        prev_hash = GENESIS_HASH
    # Hash the payload as Postgres will hand it back (jsonb normalises numbers, e.g. 1e20),
    # and take the timestamp after the lock so occurred_at follows seq order.
    occurred_at, stored_payload = conn.execute(
        text("SELECT clock_timestamp(), CAST(:payload AS jsonb)"),
        {"payload": json.dumps(payload, allow_nan=False)},
    ).one()
    digest = compute_hash(
        prev_hash=prev_hash,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        actor=actor,
        payload=stored_payload,
        occurred_at=occurred_at,
    )
    seq = conn.execute(
        text(
            "INSERT INTO audit_events "
            "(occurred_at, actor, event_type, entity_type, entity_id, payload, prev_hash, hash) "
            "VALUES (:occurred_at, :actor, :event_type, :entity_type, :entity_id, "
            "CAST(:payload AS jsonb), :prev_hash, :hash) RETURNING seq"
        ),
        {
            "occurred_at": occurred_at,
            "actor": actor,
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "payload": json.dumps(stored_payload),
            "prev_hash": prev_hash,
            "hash": digest,
        },
    ).scalar_one()
    return AuditEvent(
        seq=seq,
        occurred_at=occurred_at,
        actor=actor,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        payload=stored_payload,
        prev_hash=prev_hash,
        hash=digest,
    )


def _events(conn: Connection) -> Iterator[AuditEvent]:
    rows = conn.execute(
        text(f"SELECT {_COLUMNS} FROM audit_events ORDER BY seq").execution_options(yield_per=500)
    )
    for seq, occurred_at, actor, event_type, entity_type, entity_id, payload, prev, digest in rows:
        yield AuditEvent(
            seq=seq,
            occurred_at=occurred_at,
            actor=actor,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload,
            prev_hash=prev,
            hash=digest,
        )


def verify_chain(conn: Connection) -> VerifyResult:
    """Recompute every hash and link in seq order; report the first broken event."""
    return verify_events(_events(conn))
