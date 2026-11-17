import io
from typing import Any

import pdfplumber
import pytest

from synth.model import Invoice, MasterData, fields_to_json
from synth.render import WATERMARK, render_pdf
from tests.helpers import apply

WHITE = (1.0, 1.0, 1.0)


def _open(pdf: bytes) -> Any:
    return pdfplumber.open(io.BytesIO(pdf))


def _printed_values(inv: Invoice) -> list[str]:
    data = fields_to_json(inv.fields)
    values = [str(v) for k, v in data.items() if k != "lines"]
    for line in data["lines"]:
        values += [str(v) for v in line.values()]
    return values


def _chars_of(page: Any, needle: str) -> list[dict[str, Any]]:
    """The chars of the first run of `needle` in the page's char stream."""
    chars = [c for c in page.chars if c["text"] != " "]
    stream = "".join(c["text"] for c in chars)
    start = stream.find(needle.replace(" ", ""))
    assert start >= 0, f"{needle!r} not drawn on the page"
    return chars[start : start + len(needle.replace(" ", ""))]


def test_render_is_byte_deterministic(clean: list[Invoice]) -> None:
    assert render_pdf(clean[0]) == render_pdf(clean[0])


def test_pdf_is_small_and_single_page_with_watermark(clean: list[Invoice]) -> None:
    pdf = render_pdf(clean[0])
    assert len(pdf) < 40_000
    with _open(pdf) as doc:
        assert len(doc.pages) >= 1
        for page in doc.pages:
            marks = _chars_of(page, WATERMARK)
            assert all(c["size"] >= 20 for c in marks)
            assert all(c["non_stroking_color"] != WHITE for c in marks)


@pytest.mark.parametrize("index", [0, 7, 23, 41])
def test_every_ground_truth_value_is_printed(index: int, clean: list[Invoice]) -> None:
    inv = clean[index]
    with _open(render_pdf(inv)) as doc:
        text = "\n".join(p.extract_text() for p in doc.pages)
    for value in _printed_values(inv):
        assert value in text, value


def test_clean_invoice_has_no_instruction_anywhere(clean: list[Invoice]) -> None:
    with _open(render_pdf(clean[0])) as doc:
        text = doc.pages[0].extract_text().lower()
        meta = " ".join(str(v) for v in doc.metadata.values()).lower()
        assert "ignore previous" not in text and "ignore previous" not in meta
        assert all(c["non_stroking_color"] != WHITE for c in doc.pages[0].chars)
        assert all(c["size"] >= 6 for c in doc.pages[0].chars)


@pytest.mark.parametrize(
    "name",
    [
        "iban_swap",
        "resend_reformatted_number",
        "amount_above_tolerance",
        "unknown_vendor",
        "wrong_vat",
        "total_mismatch",
        "price_above_contract",
        "injection_visible_note",
        "injection_white_text",
        "injection_tiny_font",
        "injection_metadata",
    ],
)
def test_mutated_ground_truth_is_what_the_pdf_prints(
    name: str, master: MasterData, clean: list[Invoice]
) -> None:
    _, inv = apply(name, master, clean)
    with _open(render_pdf(inv)) as doc:
        text = "\n".join(p.extract_text() for p in doc.pages)
    for value in _printed_values(inv):
        assert value in text, value


def test_injection_visible_note_is_readable(master: MasterData, clean: list[Invoice]) -> None:
    _, inv = apply("injection_visible_note", master, clean)
    assert inv.visible_note
    with _open(render_pdf(inv)) as doc:
        chars = _chars_of(doc.pages[0], "ignore previous instructions")
    assert all(c["size"] >= 7 and c["non_stroking_color"] != WHITE for c in chars)


def test_injection_white_text_is_drawn_white(master: MasterData, clean: list[Invoice]) -> None:
    _, inv = apply("injection_white_text", master, clean)
    with _open(render_pdf(inv)) as doc:
        page = doc.pages[0]
        chars = _chars_of(page, inv.hidden_texts[0].text)
        assert chars and all(c["non_stroking_color"] == WHITE for c in chars)
        assert "ignore previous instructions" in page.extract_text().lower()


def test_injection_tiny_font_is_below_two_points(master: MasterData, clean: list[Invoice]) -> None:
    _, inv = apply("injection_tiny_font", master, clean)
    with _open(render_pdf(inv)) as doc:
        page = doc.pages[0]
        chars = _chars_of(page, inv.hidden_texts[0].text)
        assert chars and all(c["size"] < 2 for c in chars)
        assert all(c["non_stroking_color"] != WHITE for c in chars)


def test_injection_metadata_puts_the_instruction_in_subject_and_keywords(
    master: MasterData, clean: list[Invoice]
) -> None:
    _, inv = apply("injection_metadata", master, clean)
    assert inv.metadata_injection
    with _open(render_pdf(inv)) as doc:
        assert inv.metadata_injection in doc.metadata["Subject"]
        assert inv.metadata_injection in doc.metadata["Keywords"]
        assert "ignore previous" not in doc.pages[0].extract_text().lower()
