"""Helpers for the extraction and Layer-2 tests: seed ground truth and a fake-backed gateway."""

import json
from typing import Any

from opentelemetry.sdk.trace import TracerProvider

from app.config import Settings
from app.ingestion.pdf import ExtractedDocument, PdfPage, extract_document
from app.llm.gateway import LLMGateway
from app.llm.ledger import InMemoryLedger
from tests.llm_support import FAKE_KEY, FakeProvider
from tests.support import SEED_INVOICES

EXTRACT_MODEL = "gemini-3.5-flash-lite"


def seed_fields(invoice_id: str) -> dict[str, Any]:
    fields: dict[str, Any] = json.loads((SEED_INVOICES / f"{invoice_id}.json").read_text())[
        "fields"
    ]
    return fields


def seed_document(invoice_id: str) -> ExtractedDocument:
    return extract_document((SEED_INVOICES / f"{invoice_id}.pdf").read_bytes())


def text_document(visible: str, hidden: str = "") -> ExtractedDocument:
    """A one-page document whose full text adds `hidden` to what a reader sees."""
    full = f"{visible}\n{hidden}" if hidden else visible
    return ExtractedDocument(pages=(PdfPage(1, full, visible, ()),), metadata={})


def fake_gateway(provider: FakeProvider) -> LLMGateway:
    return LLMGateway(
        provider,
        None,
        InMemoryLedger(),
        Settings(_env_file=None, gemini_api_key=FAKE_KEY),  # type: ignore[call-arg]
        TracerProvider().get_tracer("test"),
        sleep=lambda s: None,
    )
