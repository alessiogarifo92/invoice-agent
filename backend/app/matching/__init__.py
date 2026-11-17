"""Matching: vendor, PO, contract and duplicate lookups that feed the rules (B1, plain SQL)."""

from app.matching.base import Matcher, compact_id, extraction_record
from app.matching.context import build_context, document_signals
from app.matching.memory import MemoryMatcher
from app.matching.sql import ClauseHit, SqlMatcher, search_contract_clauses

__all__ = [
    "ClauseHit",
    "Matcher",
    "MemoryMatcher",
    "SqlMatcher",
    "build_context",
    "compact_id",
    "document_signals",
    "extraction_record",
    "search_contract_clauses",
]
