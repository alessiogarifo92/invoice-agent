"""Hash-chain logic without a database: canonical hashing and the chain walk."""

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from app.audit import GENESIS_HASH, AuditEvent, canonical_json, compute_hash, verify_events

T0 = datetime(2026, 11, 14, 9, 30, 0, 123456, tzinfo=UTC)


def _hash(prev_hash: str, payload: dict[str, Any], occurred_at: datetime = T0) -> str:
    return compute_hash(
        prev_hash=prev_hash,
        event_type="invoice.received",
        entity_type="invoice",
        entity_id="inv-1",
        actor="api",
        payload=payload,
        occurred_at=occurred_at,
    )


def _chain(n: int) -> list[AuditEvent]:
    events: list[AuditEvent] = []
    prev = GENESIS_HASH
    for i in range(n):
        occurred_at = T0 + timedelta(seconds=i)
        payload = {"i": i}
        digest = _hash(prev, payload, occurred_at)
        events.append(
            AuditEvent(
                seq=i + 1,
                occurred_at=occurred_at,
                actor="api",
                event_type="invoice.received",
                entity_type="invoice",
                entity_id="inv-1",
                payload=payload,
                prev_hash=prev,
                hash=digest,
            )
        )
        prev = digest
    return events


def test_genesis_is_64_zeros() -> None:
    assert GENESIS_HASH == "0" * 64


def test_canonical_json_sorts_keys_and_drops_whitespace() -> None:
    assert (
        canonical_json({"b": 1, "a": {"d": [1, 2], "c": "x"}}) == b'{"a":{"c":"x","d":[1,2]},"b":1}'
    )


def test_canonical_json_keeps_non_ascii_as_utf8() -> None:
    assert canonical_json({"name": "Müller"}) == '{"name":"Müller"}'.encode()


def test_hash_is_sha256_of_the_documented_canonical_form() -> None:
    expected_input = (
        '{"actor":"api","entity_id":"inv-1","entity_type":"invoice",'
        '"event_type":"invoice.received","occurred_at":"2026-11-14T09:30:00.123456+00:00",'
        '"payload":{"a":1},"prev_hash":"' + GENESIS_HASH + '"}'
    )
    assert _hash(GENESIS_HASH, {"a": 1}) == hashlib.sha256(expected_input.encode()).hexdigest()


def test_hash_is_independent_of_payload_key_order() -> None:
    assert _hash(GENESIS_HASH, {"a": 1, "b": {"x": 1, "y": 2}}) == _hash(
        GENESIS_HASH, {"b": {"y": 2, "x": 1}, "a": 1}
    )


def test_hash_changes_with_any_field() -> None:
    base = _hash(GENESIS_HASH, {"a": 1})
    assert _hash(GENESIS_HASH, {"a": 2}) != base
    assert _hash("f" * 64, {"a": 1}) != base
    assert _hash(GENESIS_HASH, {"a": 1}, T0 + timedelta(microseconds=1)) != base


def test_hash_normalises_the_timestamp_to_utc() -> None:
    rome = timezone(timedelta(hours=1))
    assert _hash(GENESIS_HASH, {}, T0.astimezone(rome)) == _hash(GENESIS_HASH, {}, T0)


def test_hash_rejects_a_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _hash(GENESIS_HASH, {}, T0.replace(tzinfo=None))


def test_empty_chain_is_ok() -> None:
    result = verify_events([])
    assert result.ok is True
    assert result.checked == 0
    assert result.first_broken_seq is None
    assert result.reason is None
    assert result.head_hash is None


def test_intact_chain_is_ok_and_reports_the_head() -> None:
    events = _chain(5)
    result = verify_events(events)
    assert result.ok is True
    assert result.checked == 5
    assert result.first_broken_seq is None
    assert result.head_hash == events[-1].hash


def test_gaps_in_seq_are_not_a_break() -> None:
    # A rolled-back insert burns a sequence value; the chain links, not seq, carry order.
    events = [replace(e, seq=e.seq * 10) for e in _chain(3)]
    assert verify_events(events).ok is True


def test_tampered_payload_is_reported_at_that_row() -> None:
    events = _chain(5)
    events[2] = replace(events[2], payload={"i": 999})
    result = verify_events(events)
    assert result.ok is False
    assert result.first_broken_seq == 3
    assert result.reason == "hash_mismatch"
    assert result.checked == 5
    assert result.head_hash == events[-1].hash


def test_removed_row_breaks_the_next_link() -> None:
    events = _chain(5)
    del events[1]
    result = verify_events(events)
    assert result.ok is False
    assert result.first_broken_seq == 3
    assert result.reason == "prev_hash_mismatch"


def test_first_row_must_link_to_genesis() -> None:
    events = _chain(3)[1:]
    result = verify_events(events)
    assert result.ok is False
    assert result.first_broken_seq == 2
    assert result.reason == "prev_hash_mismatch"


def test_only_the_first_break_is_reported() -> None:
    events = _chain(5)
    events[1] = replace(events[1], actor="mallory")
    events[3] = replace(events[3], actor="mallory")
    assert verify_events(events).first_broken_seq == 2
