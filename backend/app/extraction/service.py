"""Extraction stage: one structured LLM call, the grounding check, and its persistence.

`extract_invoice` never touches the database; `persist_extraction` writes the outcome in the
caller's transaction, so the worker (T7) stores a stage's output atomically with its job.

Failure handling:
- SchemaError (the reply does not validate): no extraction, a `schema_failure` flag; the
  run is still recorded with the provider and model that answered (phase 3, "Agent": a
  schema failure is at least `clarify`).
- a value not printed in the visible text: one `grounding_failure` flag per field, floor
  `clarify`. The extraction is kept as returned; the flag carries the doubt.
- QuotaUnavailable propagates: the caller parks the invoice in awaiting_quota (T7). Every
  other LLMError propagates too; retrying or failing the job is the worker's decision.

`find_stored_extraction` lets the worker reuse a run already persisted for the invoice and
prompt version instead of paying for the same extraction twice.

Flags use the vocabulary of the rules (severity info/warning/critical plus an action floor).
"""

import json
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Connection, bindparam, text

from app.extraction.grounding import GroundingIssue, check_grounding
from app.extraction.prompt import PROMPT_VERSION, SYSTEM_PROMPT, build_user_message
from app.extraction.schema import InvoiceExtraction
from app.ingestion.hidden import visible_text
from app.ingestion.pdf import ExtractedDocument
from app.llm.errors import SchemaError
from app.llm.gateway import LLMGateway
from app.llm.types import Usage
from app.matching.base import extraction_record

PURPOSE = "extract"

Action = Literal["approve", "clarify", "block"]
Severity = Literal["info", "warning", "critical"]
FlagCode = Literal["schema_failure", "grounding_failure"]

# Severity and action floor per extraction flag code, so a stored run reads back as it was raised.
FLAG_POLICY: dict[str, tuple[Severity, Action]] = {
    "schema_failure": ("warning", "clarify"),
    "grounding_failure": ("warning", "clarify"),
}


@dataclass(frozen=True)
class ExtractionFlag:
    code: FlagCode
    severity: Severity
    action_floor: Action
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractionOutcome:
    extraction: InvoiceExtraction | None
    grounding_issues: list[GroundingIssue]
    flags: list[ExtractionFlag]
    provider: str
    model: str
    prompt_version: str
    usage: Usage

    @property
    def action_floor(self) -> Action:
        return "clarify" if self.flags else "approve"


def _schema_flag(provider: str) -> ExtractionFlag:
    severity, floor = FLAG_POLICY["schema_failure"]
    return ExtractionFlag(
        code="schema_failure",
        severity=severity,
        action_floor=floor,
        message="The model's reply did not match the extraction schema; nothing was extracted.",
        evidence={"provider": provider, "error": SchemaError.code},
    )


def _grounding_flag(issue: GroundingIssue) -> ExtractionFlag:
    severity, floor = FLAG_POLICY["grounding_failure"]
    return ExtractionFlag(
        code="grounding_failure",
        severity=severity,
        action_floor=floor,
        message=f"The extracted {issue.field} is not printed in the document.",
        evidence={"field": issue.field, "value": issue.value},
    )


def extract_invoice(
    gateway: LLMGateway,
    doc: ExtractedDocument,
    invoice_id: str | UUID | None,
    demo_token_hash: str | None = None,
) -> ExtractionOutcome:
    """Extract the fields from the document's visible text, then ground them in that text."""
    text_seen = visible_text(doc)
    try:
        result = gateway.complete_structured(
            PURPOSE,
            SYSTEM_PROMPT,
            build_user_message(text_seen),
            InvoiceExtraction,
            invoice_id=invoice_id,
            demo_token_hash=demo_token_hash,
        )
    except SchemaError as exc:
        return ExtractionOutcome(
            extraction=None,
            grounding_issues=[],
            flags=[_schema_flag(exc.provider)],
            provider=exc.provider,
            model=gateway.model_for(exc.provider),
            prompt_version=PROMPT_VERSION,
            usage=exc.usage,
        )
    issues = check_grounding(result.parsed, text_seen)
    return ExtractionOutcome(
        extraction=result.parsed,
        grounding_issues=issues,
        flags=[_grounding_flag(issue) for issue in issues],
        provider=result.provider,
        model=result.model,
        prompt_version=PROMPT_VERSION,
        usage=result.usage,
    )


def persist_extraction(conn: Connection, invoice_id: str | UUID, outcome: ExtractionOutcome) -> str:
    """Write the run, the extraction (if any) and its flags. Returns the run id.

    Runs in the caller's transaction. processing_runs.agent_model and finished_at stay NULL:
    the agent stage and the end of the run belong to later stages. The searchable columns
    come from `extraction_record`, so they compare like the duplicate search does (the VAT
    id compacted, the number normalised), whatever spacing the model copied.
    """
    params: dict[str, Any] = {"invoice_id": str(invoice_id)}
    run_id = str(
        conn.execute(
            text(
                "INSERT INTO processing_runs (invoice_id, provider, extraction_model, "
                "prompt_version) VALUES (CAST(:invoice_id AS uuid), :provider, :model, "
                ":prompt_version) RETURNING id"
            ),
            {
                **params,
                "provider": outcome.provider,
                "model": outcome.model,
                "prompt_version": outcome.prompt_version,
            },
        ).scalar_one()
    )
    params["run_id"] = run_id
    extraction = outcome.extraction
    if extraction is not None:
        fields = extraction.model_dump(mode="json")
        record = extraction_record(fields)
        conn.execute(
            text(
                "INSERT INTO extractions (invoice_id, run_id, fields, vendor_vat_id, "
                "invoice_number, invoice_number_norm, total, issue_date) VALUES "
                "(CAST(:invoice_id AS uuid), CAST(:run_id AS uuid), CAST(:fields AS jsonb), "
                ":vendor_vat_id, :invoice_number, :invoice_number_norm, "
                "CAST(:total AS numeric), CAST(:issue_date AS date))"
            ),
            {
                **params,
                "fields": json.dumps(fields),
                "vendor_vat_id": record.vendor_vat_id,
                "invoice_number": record.invoice_number,
                "invoice_number_norm": record.invoice_number_norm,
                "total": record.total,
                "issue_date": record.issue_date,
            },
        )
    for flag in outcome.flags:
        conn.execute(
            text(
                "INSERT INTO flags (invoice_id, run_id, code, severity, evidence) VALUES "
                "(CAST(:invoice_id AS uuid), CAST(:run_id AS uuid), :code, :severity, "
                "CAST(:evidence AS jsonb))"
            ),
            {
                **params,
                "code": flag.code,
                "severity": flag.severity,
                "evidence": json.dumps(flag.evidence),
            },
        )
    return run_id


@dataclass(frozen=True)
class StoredExtraction:
    """A persisted extraction run, read back: what the later stages need from it."""

    run_id: str
    provider: str
    model: str
    prompt_version: str
    fields: dict[str, Any] | None  # None after a schema failure
    flags: tuple[ExtractionFlag, ...]

    @property
    def action_floor(self) -> Action:
        return "clarify" if self.flags else "approve"


def load_stored_extraction(conn: Connection, run_id: str | UUID) -> StoredExtraction:
    """The run, its extraction (if any) and its extraction flags. Raises if the run is missing."""
    run = conn.execute(
        text(
            "SELECT r.id, r.provider, r.extraction_model, r.prompt_version, e.fields "
            "FROM processing_runs r LEFT JOIN extractions e ON e.run_id = r.id "
            "WHERE r.id = CAST(:run_id AS uuid)"
        ),
        {"run_id": str(run_id)},
    ).one()
    flag_rows = conn.execute(
        text(
            "SELECT code, severity, evidence FROM flags WHERE run_id = CAST(:run_id AS uuid) "
            "AND code IN :codes ORDER BY created_at, id"
        ).bindparams(bindparam("codes", expanding=True)),
        {"run_id": str(run_id), "codes": list(FLAG_POLICY)},
    ).all()
    flags = tuple(
        ExtractionFlag(
            code=row.code,
            severity=row.severity,
            action_floor=FLAG_POLICY[row.code][1],
            # The message is not stored for extraction flags; the code says what happened.
            message=row.code,
            evidence=row.evidence,
        )
        for row in flag_rows
    )
    return StoredExtraction(
        run_id=str(run.id),
        provider=run.provider,
        model=run.extraction_model,
        prompt_version=run.prompt_version,
        fields=run.fields,
        flags=flags,
    )


def find_stored_extraction(
    conn: Connection, invoice_id: str | UUID, prompt_version: str = PROMPT_VERSION
) -> StoredExtraction | None:
    """The invoice's newest run with this prompt version, or None: then the LLM must be asked."""
    run_id = conn.execute(
        text(
            "SELECT id FROM processing_runs WHERE invoice_id = CAST(:invoice_id AS uuid) "
            "AND prompt_version = :prompt_version ORDER BY started_at DESC, id DESC LIMIT 1"
        ),
        {"invoice_id": str(invoice_id), "prompt_version": prompt_version},
    ).scalar_one_or_none()
    return None if run_id is None else load_stored_extraction(conn, run_id)
