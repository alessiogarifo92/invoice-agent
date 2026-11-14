"""Append-only audit log with a tamper-evident hash chain."""

from app.audit.chain import (
    GENESIS_HASH,
    AuditEvent,
    VerifyResult,
    canonical_json,
    compute_hash,
    verify_events,
)
from app.audit.store import AUDIT_CHAIN_LOCK_KEY, append_event, verify_chain

__all__ = [
    "AUDIT_CHAIN_LOCK_KEY",
    "GENESIS_HASH",
    "AuditEvent",
    "VerifyResult",
    "append_event",
    "canonical_json",
    "compute_hash",
    "verify_chain",
    "verify_events",
]
