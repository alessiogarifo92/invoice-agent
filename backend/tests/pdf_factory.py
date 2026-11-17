"""Tiny ReportLab PDFs for the ingestion tests (synthetic text only)."""

import io

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def text_pdf(
    pages: int = 1,
    text: str = "Synthetic test invoice",
    *,
    size: float = 10,
    color: tuple[float, float, float] = (0.0, 0.0, 0.0),
    subject: str | None = None,
) -> bytes:
    """One line of `text` per page, drawn in the given size and fill colour."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, invariant=1)
    if subject is not None:
        c.setSubject(subject)
    for n in range(pages):
        c.setFont("Helvetica", size)
        c.setFillColorRGB(*color)
        c.drawString(50, 700, f"{text} {n + 1}")
        c.showPage()
    c.save()
    return buf.getvalue()


def drawing_only_pdf() -> bytes:
    """A page with lines and a rectangle but no text layer at all (like a scan)."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, invariant=1)
    c.line(50, 700, 500, 700)
    c.rect(50, 500, 200, 100)
    c.showPage()
    c.save()
    return buf.getvalue()


def mixed_pdf(lines: list[tuple[str, float, tuple[float, ...], str]]) -> bytes:
    """One drawString per entry: (text, size, fill colour, colour space 'rgb'|'gray'|'cmyk')."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, invariant=1)
    y = 750.0
    for text, size, color, space in lines:
        c.setFont("Helvetica", size)
        if space == "gray":
            c.setFillGray(color[0])
        elif space == "cmyk":
            c.setFillColorCMYK(*color)
        else:
            c.setFillColorRGB(*color)
        c.drawString(50, y, text)
        y -= 20
    c.showPage()
    c.save()
    return buf.getvalue()
