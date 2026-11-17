"""The agent's tools (phase 3, "Agent"): strict, read-only, bound to the invoice under review.

- Scope comes from the invoice, never from the model. Every lookup starts from the vendor
  matched on the invoice's own extraction (VAT id, IBAN, then name, as the rules match it)
  and from the PO number printed on it. No tool takes an invoice, vendor or PO id: only
  `search_contract_clauses` takes an argument, the search text. Every schema is closed, so
  an `invoice_id` or `vendor_id` the model adds is rejected as invalid arguments (sent back
  once for a retry, see `Limits.max_invalid_arguments`), never followed.
- A PO printed on the invoice that belongs to another vendor is confirmed to exist, and
  nothing more: its lines and billing stay out of this invoice's scope.
- Read-only twice over: the handlers only call the SELECT helpers of app.matching.sql (the
  same lookups the rules use), each inside a READ ONLY transaction.
- Every successful result carries a run-scoped `evidence_ref`, `ev-<run tag>-<n>`, the tag
  random per agent run. The proposal may cite only those; the policy gate turns any other
  ref into `clarify`. Error results carry none.
- The JSON schemas the providers see stay portable (types, enums, descriptions; strict
  modes reject length keywords). The bounds are enforced here, by Pydantic.
"""

import secrets
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Connection, Engine, text

from app.agent.text import plain_text
from app.llm.provider import InvalidToolArguments
from app.llm.types import ToolHandler, ToolSpec
from app.matching.sql import (
    bank_accounts,
    find_duplicates,
    find_vendor,
    get_po_context,
    search_contract_clauses,
    vat_rates,
)
from app.rules.checks import check_totals, check_vat
from app.rules.context import RuleContext, VendorMatch, VendorRecord
from app.rules.facts import InvoiceFacts, parse_facts
from app.rules.iban import compact_iban
from app.rules.numbers import normalize_invoice_number

PROPOSE = "propose_payment"
RATIONALE_MAX = 2000
EVIDENCE_REFS_MAX = 20
REF_MAX = 64
QUERY_MAX = 200
CLAUSE_TEXT_MAX = 1500


# --- scope and evidence -----------------------------------------------------------------------


@dataclass(frozen=True)
class InvoiceScope:
    """The invoice under review, as the tools see it. Built from the stored extraction."""

    invoice_id: str
    facts: InvoiceFacts
    vendor_match: VendorMatch | None

    @property
    def vendor(self) -> VendorRecord | None:
        return self.vendor_match.vendor if self.vendor_match is not None else None


@dataclass(frozen=True)
class Evidence:
    """One successful tool result: its ref, the record ids it cites, and what it returned."""

    ref: str
    tool: str
    records: tuple[str, ...]
    output: dict[str, Any] = field(repr=False)


@contextmanager
def read_only(engine: Engine) -> Iterator[Connection]:
    """A connection whose transaction refuses every write; rolled back at the end."""
    with engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        yield conn


def load_scope(conn: Connection, invoice_id: str, fields: Mapping[str, Any] | None) -> InvoiceScope:
    facts = parse_facts(fields or {})
    match = find_vendor(conn, facts.vendor_vat_id, facts.iban, facts.vendor_name)
    return InvoiceScope(invoice_id=invoice_id, facts=facts, vendor_match=match)


# --- argument and proposal schemas ------------------------------------------------------------


class _NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _bounded_text(max_len: int) -> Callable[[str], str]:
    def check(value: str) -> str:
        value = plain_text(value, max_len)
        if not value:
            raise ValueError("must not be empty")
        return value

    return check


class _ClauseQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Annotated[str, AfterValidator(_bounded_text(QUERY_MAX))]


Ref = Annotated[str, Field(min_length=1, max_length=REF_MAX)]


class PaymentProposal(BaseModel):
    """The terminal tool's arguments: the agent's proposal, never a payment."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "clarify", "block"]
    # Attacker-influenced prose: stored as plain text, control characters removed.
    rationale: Annotated[str, AfterValidator(_bounded_text(RATIONALE_MAX))]
    evidence_refs: list[Ref] = Field(max_length=EVIDENCE_REFS_MAX)


def _validate[M: BaseModel](schema: type[M], arguments: Mapping[str, Any]) -> M:
    try:
        return schema.model_validate(dict(arguments))
    except ValidationError as exc:
        # Where and what kind, never the value: it came from the model.
        raise InvalidToolArguments(
            [
                {"loc": ".".join(str(part) for part in err["loc"]) or "(root)", "type": err["type"]}
                for err in exc.errors()
            ]
        ) from None


def proposal_handler(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Validates propose_payment; the loop then ends with this as the proposal."""
    return _validate(PaymentProposal, arguments).model_dump()


_NO_ARGS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "get_purchase_order",
        "The purchase order printed on the invoice under review: its lines, what other "
        "invoices already billed on it, and the remaining net amount. Takes no arguments.",
        _NO_ARGS,
    ),
    ToolSpec(
        "get_vendor",
        "The vendor of the invoice under review in master data, its bank accounts (IBAN "
        "history) and whether the IBAN on the invoice is its current one. Takes no arguments.",
        _NO_ARGS,
    ),
    ToolSpec(
        "find_similar_invoices",
        "Earlier invoices of the same vendor that may be duplicates of the invoice under "
        "review (same normalised number, or same amount, close date and similar number). "
        "Takes no arguments.",
        _NO_ARGS,
    ),
    ToolSpec(
        "check_vat",
        "Recomputes the VAT rate and amount and the totals of the invoice under review "
        "against the VAT rate of the vendor's country. Takes no arguments.",
        _NO_ARGS,
    ),
    ToolSpec(
        "search_contract_clauses",
        "Full-text search in the contract clauses of the vendor of the invoice under review "
        "(for example 'price adjustment', 'credit note', 'partial delivery'). Best 5 clauses.",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": f"Search words, at most {QUERY_MAX} characters.",
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        PROPOSE,
        "Ends the review with a proposal for the human reviewer. Call it exactly once, last.",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["approve", "clarify", "block"]},
                "rationale": {
                    "type": "string",
                    "description": f"Plain text, at most {RATIONALE_MAX} characters: what was "
                    "checked, what it showed, and for clarify the question to ask.",
                },
                "evidence_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "evidence_ref values returned by tool results in this "
                    f"conversation, at most {EVIDENCE_REFS_MAX}.",
                },
            },
            "required": ["action", "rationale", "evidence_refs"],
            "additionalProperties": False,
        },
    ),
)


# --- the lookups ----------------------------------------------------------------------------

Output = tuple[dict[str, Any], tuple[str, ...]]
Lookup = Callable[[Connection, InvoiceScope, Any], Output]


def _json(value: Any) -> Any:
    """Decimals and dates as strings, tuples as lists: what goes back to the model is JSON."""
    if isinstance(value, Decimal | date):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_json(v) for v in value]
    return value


def _vendor(conn: Connection, scope: InvoiceScope, args: _NoArguments) -> Output:
    match, facts = scope.vendor_match, scope.facts
    if match is None:
        return {"found": False}, ()
    vendor = match.vendor
    accounts = bank_accounts(conn, vendor.id)
    current = {compact_iban(a.iban) for a in accounts if a.valid_to is None}
    known = {compact_iban(a.iban) for a in accounts}
    iban = compact_iban(facts.iban) if facts.iban else None
    if iban is None:
        status = "not_printed"
    else:
        status = "current" if iban in current else "past_account" if iban in known else "never_seen"
    output = {
        "found": True,
        "match_method": match.method,
        "match_score": round(match.score, 3),
        "vendor": {
            "id": vendor.id,
            "name": vendor.name,
            "vat_id": vendor.vat_id,
            "country": vendor.country,
        },
        "bank_accounts": [
            {
                "iban": a.iban,
                "valid_from": a.valid_from,
                "valid_to": a.valid_to,
                "current": a.valid_to is None,
            }
            for a in accounts
        ],
        "invoice_iban": iban,
        "invoice_iban_status": status,
    }
    return output, (vendor.id,)


def _purchase_order(conn: Connection, scope: InvoiceScope, args: _NoArguments) -> Output:
    po_number = scope.facts.po_number
    if not po_number:
        return {"found": False, "reason": "no_po_number"}, ()
    po = get_po_context(conn, po_number, exclude_invoice_id=scope.invoice_id)
    if po is None:
        return {"found": False, "reason": "not_in_master_data", "po_number": po_number}, ()
    vendor = scope.vendor
    if vendor is None or po.vendor_id != vendor.id:
        return {"found": True, "id": po.id, "belongs_to_invoice_vendor": False}, (po.id,)
    output = {
        "found": True,
        "id": po.id,
        "belongs_to_invoice_vendor": True,
        "currency": po.currency,
        "issue_date": po.issue_date,
        "total_net": po.total_net,
        "lines": [
            {"line_no": n.line_no, "sku": n.sku, "quantity": n.quantity, "unit_price": n.unit_price}
            for n in po.lines
        ],
        "billed_before": po.billed_before,
        "billed_invoice_ids": po.billed_invoice_ids,
        "billed_unreadable_invoice_ids": po.billed_unreadable_ids,
        "remaining_net": po.total_net - po.billed_before,
        "this_invoice_net": scope.facts.subtotal,
    }
    return output, (po.id, *po.billed_invoice_ids)


def _similar_invoices(conn: Connection, scope: InvoiceScope, args: _NoArguments) -> Output:
    vendor, facts = scope.vendor, scope.facts
    if vendor is None:
        return {"vendor_known": False, "candidates": []}, ()
    norm = normalize_invoice_number(facts.invoice_number) if facts.invoice_number else ""
    if not norm:
        return {"vendor_known": True, "invoice_number_known": False, "candidates": []}, ()
    candidates = find_duplicates(
        conn, vendor.id, norm, facts.total, facts.issue_date, exclude_invoice_id=scope.invoice_id
    )
    output = {
        "vendor_known": True,
        "invoice_number_known": True,
        "candidates": [
            {
                "invoice_id": c.invoice_id,
                "invoice_number": c.invoice_number,
                "total": c.total,
                "issue_date": c.issue_date,
                "match": c.match,
                "number_similarity": round(c.similarity, 3),
            }
            for c in candidates
        ],
    }
    return output, tuple(c.invoice_id for c in candidates)


def _vat(conn: Connection, scope: InvoiceScope, args: _NoArguments) -> Output:
    facts, vendor = scope.facts, scope.vendor
    rates = vat_rates(conn)
    ctx = RuleContext(vendor_match=scope.vendor_match, vat_rates=rates)
    # Master data is the reference for a known vendor, the printed country only otherwise.
    country = vendor.country if vendor is not None else facts.vendor_country
    findings = [*check_vat(facts, ctx), *check_totals(facts, ctx)]
    output = {
        "country": country,
        "country_source": None if country is None else "master_data" if vendor else "invoice",
        "expected_rate_pct": rates.get(country.upper()) if country else None,
        "invoice_rate_pct": facts.vat_rate,
        "subtotal": facts.subtotal,
        "vat_amount": facts.vat_amount,
        "total": facts.total,
        "findings": [{"code": f.code, "message": f.message, **f.evidence} for f in findings],
    }
    return output, ((f"vat_rates/{country.upper()}",) if country else ())


def _clauses(conn: Connection, scope: InvoiceScope, args: _ClauseQuery) -> Output:
    vendor = scope.vendor
    if vendor is None:
        return {"vendor_known": False, "clauses": []}, ()
    hits = search_contract_clauses(conn, vendor.id, args.query)
    output = {
        "vendor_known": True,
        "clauses": [
            {
                "contract_id": h.contract_id,
                "clause_no": h.clause_no,
                "title": h.title,
                "text": h.text[:CLAUSE_TEXT_MAX],
                "rank": round(h.rank, 4),
            }
            for h in hits
        ],
    }
    return output, tuple(f"{h.contract_id}/{h.clause_no}" for h in hits)


_LOOKUPS: dict[str, tuple[type[BaseModel], Lookup]] = {
    "get_purchase_order": (_NoArguments, _purchase_order),
    "get_vendor": (_NoArguments, _vendor),
    "find_similar_invoices": (_NoArguments, _similar_invoices),
    "check_vat": (_NoArguments, _vat),
    "search_contract_clauses": (_ClauseQuery, _clauses),
}


class InvoiceTools:
    """The tools of one agent run over one invoice, and the evidence they returned."""

    def __init__(self, engine: Engine, scope: InvoiceScope, *, ref_tag: str | None = None):
        self._engine = engine
        self.scope = scope
        self._tag = ref_tag or secrets.token_hex(2)
        self.evidence: list[Evidence] = []

    @property
    def specs(self) -> list[ToolSpec]:
        return list(TOOL_SPECS)

    @property
    def handlers(self) -> dict[str, ToolHandler]:
        handlers = {name: self._handler(name) for name in _LOOKUPS}
        handlers[PROPOSE] = proposal_handler
        return handlers

    @property
    def returned_refs(self) -> frozenset[str]:
        return frozenset(e.ref for e in self.evidence)

    def _handler(self, name: str) -> ToolHandler:
        schema, lookup = _LOOKUPS[name]

        def handler(arguments: dict[str, Any]) -> dict[str, Any]:
            args = _validate(schema, arguments)
            with read_only(self._engine) as conn:
                output, records = lookup(conn, self.scope, args)
            ref = f"ev-{self._tag}-{len(self.evidence) + 1}"
            output = _json(output)
            self.evidence.append(Evidence(ref, name, records, output))
            return {"evidence_ref": ref, **output}

        return handler
