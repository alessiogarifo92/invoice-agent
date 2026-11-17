"""Assemble a RuleContext from a matcher, the invoice's facts and its document signals."""

from collections.abc import Sequence

from app.ingestion import ExtractedDocument, HiddenTextFinding, all_text, detect_hidden_text
from app.matching.base import Matcher
from app.rules.context import DocumentFinding, RuleContext
from app.rules.facts import InvoiceFacts
from app.rules.numbers import normalize_invoice_number
from app.rules.sweep import TextSweep, sweep_text


def document_signals(doc: ExtractedDocument) -> tuple[list[HiddenTextFinding], TextSweep]:
    """Hidden-text findings, and the regex sweep over every glyph plus every metadata value.

    `all_text` covers page glyphs only, so metadata is swept separately: an injection hidden
    in the PDF Subject or Keywords still has its IBAN checked.
    """
    return detect_hidden_text(doc), sweep_text(all_text(doc), doc.metadata)


def build_context(
    matcher: Matcher,
    facts: InvoiceFacts,
    invoice_id: str | None,
    document_findings: Sequence[DocumentFinding] = (),
    text_sweep: TextSweep | None = None,
) -> RuleContext:
    """Every lookup the rules need. `invoice_id` is left out of duplicates and PO billing.

    Lookups whose key is missing are skipped, not guessed: the rules see the gap and raise
    their own flag (unknown vendor, PO not found, missing field).
    """
    match = matcher.find_vendor(facts.vendor_vat_id, facts.iban, facts.vendor_name)
    vendor = match.vendor if match is not None else None
    po = (
        matcher.get_po_context(facts.po_number, exclude_invoice_id=invoice_id)
        if facts.po_number
        else None
    )
    norm = normalize_invoice_number(facts.invoice_number) if facts.invoice_number else ""
    return RuleContext(
        vendor_match=match,
        bank_accounts=matcher.bank_accounts(vendor.id) if vendor is not None else (),
        po=po,
        contract_prices=(
            matcher.contract_prices(vendor.id, facts.issue_date)
            if vendor is not None and facts.issue_date is not None
            else ()
        ),
        vat_rates=matcher.vat_rates(),
        po_tolerance_pct=matcher.po_tolerance_pct(),
        duplicates=(
            matcher.find_duplicates(
                vendor.id, norm, facts.total, facts.issue_date, exclude_invoice_id=invoice_id
            )
            if vendor is not None and norm
            else ()
        ),
        document_findings=tuple(document_findings),
        text_sweep=text_sweep if text_sweep is not None else TextSweep(),
    )
