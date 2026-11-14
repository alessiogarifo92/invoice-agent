"""PDF text extraction with pdfplumber: per-page text, per-glyph records and metadata.

Everything here is CPU-bound and synchronous. Callers on the event loop run it in a thread
(run_in_threadpool / anyio.to_thread), never inline.
"""

import io
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pdfplumber
from pdfplumber.page import Page
from pdfplumber.pdf import PDF

from app.ingestion.visibility import hidden_kind


class UnreadablePdf(Exception):
    """The bytes could not be parsed as a PDF with at least one page."""


@dataclass(frozen=True)
class PdfChar:
    text: str
    fontsize: float
    # Fill colour as pdfplumber reports it: 1 (gray), 3 (RGB) or 4 (CMYK) components.
    # None for pattern or otherwise non-numeric fills.
    non_stroking_color: tuple[float, ...] | None
    x0: float
    top: float


@dataclass(frozen=True)
class PdfPage:
    number: int  # 1-based
    text: str  # every glyph, hidden ones included
    visible_text: str  # hidden glyphs (white fill, tiny font) removed
    chars: tuple[PdfChar, ...]


@dataclass(frozen=True)
class ExtractedDocument:
    pages: tuple[PdfPage, ...]
    # PDF Info dictionary, string values only (Title, Author, Subject, Keywords, ...).
    metadata: dict[str, str]

    @property
    def page_count(self) -> int:
        return len(self.pages)


@contextmanager
def open_pdf(data: bytes) -> Iterator[PDF]:
    """Open `data` with pdfplumber; any parsing failure surfaces as UnreadablePdf.

    pdfminer parses lazily and raises a wide range of exception types (syntax errors, EOF,
    KeyError on a broken xref...), also while the caller reads pages, so the body of the
    `with` block is covered too.
    """
    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as exc:
        raise UnreadablePdf(f"cannot open PDF: {exc!r}") from exc
    with pdf:
        try:
            if not pdf.pages:
                raise UnreadablePdf("the PDF has no pages")
            yield pdf
        except UnreadablePdf:
            raise
        except Exception as exc:
            raise UnreadablePdf(f"cannot parse PDF: {exc!r}") from exc


def extract_document(data: bytes) -> ExtractedDocument:
    with open_pdf(data) as pdf:
        return ExtractedDocument(
            pages=tuple(_page(page) for page in pdf.pages),
            metadata=_metadata(pdf.metadata),
        )


def _page(page: Page) -> PdfPage:
    chars = tuple(_char(c) for c in page.chars)

    def is_visible(obj: dict[str, Any]) -> bool:
        if obj.get("object_type") != "char":
            return True
        return hidden_kind(obj["text"], obj["size"], _color(obj.get("non_stroking_color"))) is None

    return PdfPage(
        number=page.page_number,
        text=page.extract_text(),
        visible_text=page.filter(is_visible).extract_text(),
        chars=chars,
    )


def _char(obj: dict[str, Any]) -> PdfChar:
    return PdfChar(
        text=obj["text"],
        fontsize=float(obj["size"]),
        non_stroking_color=_color(obj.get("non_stroking_color")),
        x0=float(obj["x0"]),
        top=float(obj["top"]),
    )


def _color(value: object) -> tuple[float, ...] | None:
    if isinstance(value, int | float):
        return (float(value),)
    if isinstance(value, tuple | list) and value and all(isinstance(v, int | float) for v in value):
        return tuple(float(v) for v in value)
    return None


def _metadata_value(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, list | tuple):
        parts = [p for p in (_metadata_value(v) for v in value) if p]
        return " ".join(parts) or None
    return None


def _metadata(raw: dict[str, Any]) -> dict[str, str]:
    metadata = {}
    for key, value in raw.items():
        text = _metadata_value(value)
        if text is not None:
            metadata[str(key)] = text
    return metadata
