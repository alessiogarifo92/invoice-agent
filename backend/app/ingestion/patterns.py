"""Instruction-like phrases that have no business in a supplier invoice.

The single list of patterns the hidden-text detector (and later the rules) sweep for. Each one
is case-insensitive and lets whitespace include line breaks, since extracted text wraps.
Patterns are deliberately narrow: "Please disregard this reminder" or "If you are not the
intended recipient" are ordinary invoice prose and must not raise a flag.
"""

import re
from dataclasses import dataclass

_I = re.IGNORECASE
# Filler words allowed between a verb and its object ("ignore all of the previous ...").
_FILLER = r"(?:(?:all|any|of|the|previous|prior|above|earlier|your|these|those|other)\s+)*"

INSTRUCTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "override_instructions",
        re.compile(
            rf"\b(?:ignore|disregard|forget|override)\s+{_FILLER}"
            r"(?:instructions?|rules|prompts?|guidelines|checks)\b",
            _I,
        ),
    ),
    ("approve_payment", re.compile(r"\bapprove\s+(?:the\s+|this\s+)?payment\b", _I)),
    (
        "role_reassignment",
        re.compile(
            r"\byou\s+are\s+now\b"
            r"|\byou\s+are\s+(?:a|an|the)\s+(?:\w+\s+){0,3}(?:assistant|ai|agent|model|bot)\b",
            _I,
        ),
    ),
    ("system_prompt", re.compile(r"\bsystem\s+prompt\b", _I)),
    # A chat-transcript role label at the start of a line ("SYSTEM: ...").
    ("role_marker", re.compile(r"^\s*(?:system|assistant|developer)\s*:", _I | re.MULTILINE)),
    ("new_instructions", re.compile(r"\bnew\s+instructions\b", _I)),
    (
        "pay_to_account",
        re.compile(
            r"\bpay\s+(?:(?:it|this|the|invoice|amount)\s+){0,3}(?:in)?to\s+"
            r"(?:(?:the|this|our|a|new|following)\s+){0,3}(?:iban|(?:bank\s+)?account)\b",
            _I,
        ),
    ),
    (
        "bank_details_changed",
        re.compile(
            r"\b(?:bank|banking|account|payment)\s+(?:details|information|info|coordinates)\s+"
            r"(?:have|has)\s+(?:been\s+)?(?:changed|updated)\b"
            r"|\bnew\s+(?:iban|bank\s+account|bank\s+details)\b",
            _I,
        ),
    ),
    (
        "suppress_checks",
        re.compile(
            r"\bdo\s+not\s+(?:flag|check|verify|review|report)\b"
            r"|\bwithout\s+(?:any\s+)?(?:further\s+)?(?:checks|verification|review)\b"
            r"|\bmark\s+(?:this\s+)?(?:invoice\s+)?as\s+(?:verified|approved|paid)\b",
            _I,
        ),
    ),
    (
        "addressed_to_ai",
        re.compile(
            r"\b(?:ai|automated|virtual)\s+(?:\w+\s+)?(?:assistant|agent)\b"
            r"|\b(?:language\s+model|llm)\b",
            _I,
        ),
    ),
)


@dataclass(frozen=True)
class InstructionMatch:
    pattern: str
    text: str
    start: int
    end: int


def find_instructions(text: str) -> list[InstructionMatch]:
    """Every instruction-like phrase in `text`, in text order."""
    matches = [
        InstructionMatch(pattern=name, text=m.group(0), start=m.start(), end=m.end())
        for name, regex in INSTRUCTION_PATTERNS
        for m in regex.finditer(text)
    ]
    return sorted(matches, key=lambda m: (m.start, m.end))
