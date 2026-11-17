"""Agent prose as plain text (phase 3, "Agent": it is attacker-influenced).

The rationale is stored and shown as plain text: nothing here escapes or strips markup,
because nothing downstream may render it. What is removed are the characters that can
hide or reorder text on screen or in a log: control characters (newline and tab stay),
format characters such as bidi overrides and zero-width spaces, and surrogates.
"""

import re
import unicodedata

_KEPT_CONTROLS = frozenset("\n\t")
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})
_BLANK_LINES = re.compile(r"\n{3,}")
_TRAILING_SPACE = re.compile(r"[ \t]+\n")
ELLIPSIS = "…"


def plain_text(value: str, max_len: int) -> str:
    """`value` without hidden characters, at most two newlines in a row, stripped, and cut
    to `max_len` characters (the last one an ellipsis when it was cut)."""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    kept = "".join(
        ch
        for ch in value
        if ch in _KEPT_CONTROLS or unicodedata.category(ch) not in _DROPPED_CATEGORIES
    )
    kept = _BLANK_LINES.sub("\n\n", _TRAILING_SPACE.sub("\n", kept)).strip()
    if len(kept) > max_len:
        kept = kept[: max_len - 1].rstrip() + ELLIPSIS
    return kept
