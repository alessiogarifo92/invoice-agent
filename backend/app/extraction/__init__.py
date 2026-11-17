"""Structured extraction of invoice fields from the visible PDF text, plus the grounding check."""

from app.extraction.grounding import GroundingIssue, check_grounding
from app.extraction.prompt import PROMPT_VERSION, SYSTEM_PROMPT, build_user_message
from app.extraction.schema import InvoiceExtraction, LineExtraction
from app.extraction.service import (
    ExtractionFlag,
    ExtractionOutcome,
    extract_invoice,
    persist_extraction,
)

__all__ = [
    "PROMPT_VERSION",
    "SYSTEM_PROMPT",
    "ExtractionFlag",
    "ExtractionOutcome",
    "GroundingIssue",
    "InvoiceExtraction",
    "LineExtraction",
    "build_user_message",
    "check_grounding",
    "extract_invoice",
    "persist_extraction",
]
