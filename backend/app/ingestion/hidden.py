"""Hidden-text and prompt-injection detector over an extracted PDF.

Pure functions over an `ExtractedDocument`: nothing is written to the database here (the
pipeline turns findings into flags). Findings never carry more than a 200-char snippet.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from app.ingestion.patterns import InstructionMatch, find_instructions
from app.ingestion.pdf import ExtractedDocument, PdfChar, PdfPage
from app.ingestion.visibility import HiddenKind, hidden_kind

FindingKind = Literal["white_text", "tiny_font", "metadata_instruction", "instruction_like_text"]

SNIPPET_MAX = 200
PAGE_SEPARATOR = "\n\n"
# A hidden run counts only if it spells at least one word, not a stray glyph or a dot.
_WORD = re.compile(r"\w{2,}")


@dataclass(frozen=True)
class HiddenTextFinding:
    kind: FindingKind
    page: int | None  # 1-based; None for document metadata
    snippet: str
    evidence: dict[str, Any] = field(default_factory=dict)


def visible_text(doc: ExtractedDocument) -> str:
    """What a human reader sees: hidden glyphs excluded. This is the text an LLM may get."""
    return PAGE_SEPARATOR.join(page.visible_text for page in doc.pages)


def all_text(doc: ExtractedDocument) -> str:
    """Every glyph, hidden ones included: the input for the rules' regex sweep."""
    return PAGE_SEPARATOR.join(page.text for page in doc.pages)


def detect_hidden_text(doc: ExtractedDocument) -> list[HiddenTextFinding]:
    findings: list[HiddenTextFinding] = []
    for page in doc.pages:
        findings += _hidden_runs(page)
        findings += _visible_instructions(page)
    findings += _metadata_instructions(doc.metadata)
    return findings


def _snippet(text: str) -> str:
    return " ".join(text.split())[:SNIPPET_MAX]


def _pattern_names(matches: list[InstructionMatch]) -> list[str]:
    return sorted({m.pattern for m in matches})


@dataclass
class _Run:
    kind: HiddenKind
    chars: list[PdfChar]
    parts: list[str]


def _hidden_runs(page: PdfPage) -> list[HiddenTextFinding]:
    """Consecutive hidden glyphs of one kind, in content-stream order, as one finding each.

    Whitespace neither starts nor breaks a run, so a hidden sentence wrapped over several
    lines (each its own text-drawing operation) stays a single finding.
    """
    runs: list[_Run] = []
    current: _Run | None = None
    for char in page.chars:
        if not char.text.strip():
            if current is not None:
                current.parts.append(" ")
            continue
        kind = hidden_kind(char.text, char.fontsize, char.non_stroking_color)
        if kind is None:
            current = None
            continue
        if current is None or current.kind != kind:
            current = _Run(kind=kind, chars=[], parts=[])
            runs.append(current)
        elif _starts_new_line(current.chars[-1], char):
            current.parts.append(" ")
        current.chars.append(char)
        current.parts.append(char.text)

    findings = []
    for run in runs:
        text = " ".join("".join(run.parts).split())
        if not _WORD.search(text):
            continue
        first = run.chars[0]
        findings.append(
            HiddenTextFinding(
                kind=run.kind,
                page=page.number,
                snippet=text[:SNIPPET_MAX],
                evidence={
                    "chars": len(run.chars),
                    "fill_color": list(first.non_stroking_color)
                    if first.non_stroking_color is not None
                    else None,
                    "min_fontsize": min(c.fontsize for c in run.chars),
                    "x0": first.x0,
                    "top": first.top,
                    "patterns": _pattern_names(find_instructions(text)),
                },
            )
        )
    return findings


def _starts_new_line(prev: PdfChar, char: PdfChar) -> bool:
    return char.x0 < prev.x0 or abs(char.top - prev.top) > max(prev.fontsize, 1.0) / 2


def _visible_instructions(page: PdfPage) -> list[HiddenTextFinding]:
    matches = find_instructions(page.visible_text)
    if not matches:
        return []
    # The snippet starts at the beginning of the line holding the first match.
    line_start = page.visible_text.rfind("\n", 0, matches[0].start) + 1
    return [
        HiddenTextFinding(
            kind="instruction_like_text",
            page=page.number,
            snippet=_snippet(page.visible_text[line_start:]),
            evidence={
                "patterns": _pattern_names(matches),
                "matches": [_snippet(m.text) for m in matches[:10]],
            },
        )
    ]


def _metadata_instructions(metadata: dict[str, str]) -> list[HiddenTextFinding]:
    findings = []
    for key in sorted(metadata):
        matches = find_instructions(metadata[key])
        if matches:
            findings.append(
                HiddenTextFinding(
                    kind="metadata_instruction",
                    page=None,
                    snippet=_snippet(metadata[key]),
                    evidence={"field": key, "patterns": _pattern_names(matches)},
                )
            )
    return findings
