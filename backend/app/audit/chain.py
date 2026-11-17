"""Pure hash-chain logic: canonical hashing and the chain walk. No database here."""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class AuditEvent:
    seq: int
    occurred_at: datetime
    actor: str
    event_type: str
    entity_type: str
    entity_id: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    # Number of events walked (the whole table, even past a break).
    checked: int
    first_broken_seq: int | None
    # "hash_mismatch" (a row's content changed) or "prev_hash_mismatch" (a link is broken).
    reason: str | None
    # Stored hash of the last event: the value published externally as the chain head.
    head_hash: str | None


def canonical_json(value: Any) -> bytes:
    """Sorted keys, no whitespace, UTF-8: the same value always gives the same bytes."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def compute_hash(
    *,
    prev_hash: str,
    event_type: str,
    entity_type: str,
    entity_id: str,
    actor: str,
    payload: dict[str, Any],
    occurred_at: datetime,
) -> str:
    if occurred_at.tzinfo is None:
        raise ValueError("occurred_at must be timezone-aware")
    document = {
        "prev_hash": prev_hash,
        "event_type": event_type,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "actor": actor,
        "payload": payload,
        # UTC with microseconds, the precision of timestamptz, so a DB round trip is exact.
        "occurred_at": occurred_at.astimezone(UTC).isoformat(timespec="microseconds"),
    }
    return hashlib.sha256(canonical_json(document)).hexdigest()


def event_hash(event: AuditEvent) -> str:
    return compute_hash(
        prev_hash=event.prev_hash,
        event_type=event.event_type,
        entity_type=event.entity_type,
        entity_id=event.entity_id,
        actor=event.actor,
        payload=event.payload,
        occurred_at=event.occurred_at,
    )


def verify_events(events: Iterable[AuditEvent]) -> VerifyResult:
    """Walk events in seq order: every hash must match its content and link to the previous."""
    checked = 0
    expected_prev = GENESIS_HASH
    first_broken_seq: int | None = None
    reason: str | None = None
    for event in events:
        checked += 1
        if first_broken_seq is None:
            if event.prev_hash != expected_prev:
                first_broken_seq, reason = event.seq, "prev_hash_mismatch"
            elif event_hash(event) != event.hash:
                first_broken_seq, reason = event.seq, "hash_mismatch"
        expected_prev = event.hash
    return VerifyResult(
        ok=first_broken_seq is None,
        checked=checked,
        first_broken_seq=first_broken_seq,
        reason=reason,
        head_hash=expected_prev if checked else None,
    )
