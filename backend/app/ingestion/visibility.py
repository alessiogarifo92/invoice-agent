"""Which glyphs a human reader cannot see: near-white fill or a font too small to read.

Shared by the extractor (to build the visible text) and the hidden-text detector, so both
agree on what "hidden" means.
"""

from typing import Literal

HiddenKind = Literal["white_text", "tiny_font"]

# A fill is near-white when every RGB (or gray) component is at least this bright.
WHITE_THRESHOLD = 0.95
# Below this size (in points) text is unreadable on a printed or rendered page.
TINY_FONT_PT = 3.0


def is_near_white(color: tuple[float, ...] | None) -> bool:
    """Gray (1 component), RGB (3) or CMYK (4, converted to RGB) at or above the threshold."""
    if not color:
        return False
    if len(color) == 1:
        rgb: tuple[float, ...] = (color[0],) * 3
    elif len(color) == 3:
        rgb = color
    elif len(color) == 4:
        c, m, y, k = color
        rgb = ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    else:
        return False
    return all(component >= WHITE_THRESHOLD for component in rgb)


def hidden_kind(text: str, fontsize: float, color: tuple[float, ...] | None) -> HiddenKind | None:
    """Why this glyph is hidden, or None. Whitespace is never hidden: it carries no text.

    A glyph that is both tiny and white counts as tiny_font.
    """
    if not text.strip():
        return None
    if fontsize < TINY_FONT_PT:
        return "tiny_font"
    if is_near_white(color):
        return "white_text"
    return None
