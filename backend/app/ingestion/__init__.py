"""PDF ingestion: upload validation, text extraction and the hidden-text detector."""

from app.ingestion.hidden import HiddenTextFinding, all_text, detect_hidden_text, visible_text
from app.ingestion.patterns import INSTRUCTION_PATTERNS, InstructionMatch, find_instructions
from app.ingestion.pdf import ExtractedDocument, PdfChar, PdfPage, UnreadablePdf, extract_document

__all__ = [
    "INSTRUCTION_PATTERNS",
    "ExtractedDocument",
    "HiddenTextFinding",
    "InstructionMatch",
    "PdfChar",
    "PdfPage",
    "UnreadablePdf",
    "all_text",
    "detect_hidden_text",
    "extract_document",
    "find_instructions",
    "visible_text",
]
