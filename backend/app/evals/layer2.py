"""Layer-2 eval: live extraction on a fixed seed subset, compared with the ground truth.

Phase 3, "Evals": every PR that touches extraction, the LLM gateway or the evals runs it in
CI with the CI key, about 25 calls on the free tier. Usage (from backend/):

    uv run python -m app.evals.layer2 --seed-dir ../data/seed \\
        --baseline ../evals/baseline.json --limit 25 [--write-baseline]

- The subset is deterministic: the first invoice (by id) of every anomaly type, then the
  clean invoices by id, then the remaining anomalous ones, cut at --limit.
- Each field is compared with normalised equality (the grounding normalisation: case and
  whitespace for text, spacing for ids, numeric value for amounts); `lines` is one field,
  equal only when every line matches in order. No extraction counts every field wrong.
- Gate: fail when overall accuracy < baseline layer2.overall - 0.02, when any field drops
  more than 0.10 below its baseline, or when more than 20% of the calls fail schema
  validation (or fail with another provider error). Exhausted quota stops the run and
  passes with a warning (status "quota_exhausted"): the free tier, not the code, ran out.
- No layer2 baseline yet: the run passes and prints its numbers. The report carries the block
  to commit (`baseline_candidate`); commit it into evals/baseline.json as "layer2" from the
  first CI run's artifact, or run with --write-baseline where a key is available. The file
  is shared with Layer 1 ({"layer1": ..., "layer2": ...}); only "layer2" is ever rewritten.

Exit codes: 0 pass, 1 gate failure, 2 configuration error (e.g. no API key).
"""

import argparse
import json
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from app.config import Settings
from app.extraction.grounding import FIELD_KINDS, LINE_KINDS, Kind
from app.extraction.normalize import compact, normalize_text
from app.extraction.prompt import PROMPT_VERSION
from app.extraction.schema import InvoiceExtraction
from app.extraction.service import extract_invoice
from app.ingestion.pdf import extract_document
from app.llm.errors import LLMError, QuotaUnavailable
from app.llm.gateway import GatewayConfigError, LLMGateway, build_gateway
from app.llm.ledger import InMemoryLedger
from app.llm.types import Usage

REPO = Path(__file__).resolve().parents[3]
DEFAULT_SEED_DIR = REPO / "data" / "seed"
DEFAULT_BASELINE = REPO / "evals" / "baseline.json"
DEFAULT_REPORT = REPO / "evals" / "out" / "layer2-report.json"

DEFAULT_LIMIT = 25
# Free tiers allow a handful of requests per minute: space the calls out.
DEFAULT_DELAY_S = 4.0
MAX_OVERALL_DROP = 0.02
MAX_FIELD_DROP = 0.10
MAX_ERROR_RATE = 0.20
# Float noise must not turn an exact-threshold score into a failure.
_EPSILON = 1e-9

FIELDS: list[str] = list(InvoiceExtraction.model_fields)

Status = Literal[
    "ok", "no_baseline", "regression", "quota_exhausted", "schema_failures", "llm_errors"
]


def load_index(seed_dir: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = json.loads((seed_dir / "index.json").read_text())["invoices"]
    return entries


def select_subset(index: Sequence[dict[str, Any]], limit: int) -> list[str]:
    """One invoice per anomaly type, then clean invoices, then the other anomalous ones."""
    entries = sorted(index, key=lambda e: str(e["invoice_id"]))
    chosen: list[str] = []
    seen_types: set[str] = set()
    for entry in entries:
        new_types = set(entry["anomalies"]) - seen_types
        if new_types:
            chosen.append(entry["invoice_id"])
            seen_types |= set(entry["anomalies"])
    chosen += [e["invoice_id"] for e in entries if not e["anomalies"]]
    chosen += [e["invoice_id"] for e in entries if e["invoice_id"] not in chosen]
    return chosen[:limit]


def _equal(kind: Kind, expected: Any, got: Any) -> bool:
    if expected is None or got is None:
        return expected is None and got is None
    if kind == "number":
        try:
            return Decimal(str(expected)) == Decimal(str(got))
        except InvalidOperation:
            return False
    if kind == "id":
        return compact(str(expected)) == compact(str(got))
    if kind == "text":
        return normalize_text(str(expected)) == normalize_text(str(got))
    return str(expected) == str(got)


def _lines_equal(expected: list[dict[str, Any]], got: list[dict[str, Any]]) -> bool:
    return len(expected) == len(got) and all(
        _equal(kind, e.get(name), g.get(name))
        for e, g in zip(expected, got, strict=True)
        for name, kind in LINE_KINDS.items()
    )


def compare_fields(expected: dict[str, Any], got: InvoiceExtraction | None) -> dict[str, bool]:
    """Field name -> whether the extraction matches the ground truth."""
    if got is None:
        return dict.fromkeys(FIELDS, False)
    values = got.model_dump(mode="json")
    result = {
        name: _equal(kind, expected.get(name), values[name]) for name, kind in FIELD_KINDS.items()
    }
    result["lines"] = _lines_equal(expected.get("lines") or [], values["lines"])
    return {name: result[name] for name in FIELDS}


@dataclass
class InvoiceResult:
    invoice_id: str
    anomalies: list[str]
    fields: dict[str, bool]
    error: str | None = None
    grounding_issues: int = 0
    mismatches: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Layer2Run:
    provider: str
    model: str
    results: list[InvoiceResult] = field(default_factory=list)
    quota_exhausted: bool = False
    usage: Usage = field(default_factory=Usage)
    per_field: dict[str, float] = field(default_factory=dict)
    overall: float | None = None

    @property
    def attempted(self) -> int:
        return len(self.results)

    @property
    def errors(self) -> dict[str, int]:
        return dict(Counter(r.error for r in self.results if r.error))

    def rate(self, error: str, *, exclude: bool = False) -> float:
        if not self.results:
            return 0.0
        hits = [r for r in self.results if r.error and ((r.error == error) != exclude)]
        return len(hits) / len(self.results)

    def score(self) -> None:
        if not self.results:
            self.per_field, self.overall = {}, None
            return
        n = len(self.results)
        self.per_field = {
            name: round(sum(r.fields[name] for r in self.results) / n, 4) for name in FIELDS
        }
        total = sum(sum(r.fields.values()) for r in self.results)
        self.overall = round(total / (n * len(FIELDS)), 4)

    def baseline_block(self) -> dict[str, Any]:
        return {
            "overall": self.overall,
            "fields": self.per_field,
            "invoices": self.attempted,
            "provider": self.provider,
            "model": self.model,
            "prompt_version": PROMPT_VERSION,
        }


def _mismatches(expected: dict[str, Any], got: InvoiceExtraction, ok: dict[str, bool]) -> list[Any]:
    values = got.model_dump(mode="json")
    return [
        {"field": name, "expected": expected.get(name), "got": values[name]}
        for name, matched in ok.items()
        if not matched
    ]


def run_layer2(
    gateway: LLMGateway,
    seed_dir: Path,
    invoice_ids: Sequence[str],
    *,
    delay_s: float = DEFAULT_DELAY_S,
    sleep: Callable[[float], None] = time.sleep,
) -> Layer2Run:
    """Extract each invoice in turn; stops at the first QuotaUnavailable."""
    primary = gateway.providers[0].name
    run = Layer2Run(provider=primary, model=gateway.model_for(primary))
    anomalies = {e["invoice_id"]: list(e["anomalies"]) for e in load_index(seed_dir)}
    for position, invoice_id in enumerate(invoice_ids):
        if position:
            sleep(delay_s)
        truth = json.loads((seed_dir / "invoices" / f"{invoice_id}.json").read_text())
        expected: dict[str, Any] = truth["fields"]
        doc = extract_document((seed_dir / "invoices" / f"{invoice_id}.pdf").read_bytes())
        result = InvoiceResult(invoice_id, anomalies.get(invoice_id, []), {})
        try:
            outcome = extract_invoice(gateway, doc, None)
        except QuotaUnavailable:
            run.quota_exhausted = True
            break
        except LLMError as exc:
            run.usage = run.usage + exc.usage
            result.error = type(exc).__name__
            result.fields = compare_fields(expected, None)
            run.results.append(result)
            continue
        run.usage = run.usage + outcome.usage
        result.fields = compare_fields(expected, outcome.extraction)
        result.grounding_issues = len(outcome.grounding_issues)
        if outcome.extraction is None:
            result.error = "SchemaError"
        else:
            result.mismatches = _mismatches(expected, outcome.extraction, result.fields)
        run.results.append(result)
    run.score()
    return run


@dataclass(frozen=True)
class Verdict:
    status: Status
    passed: bool
    reasons: list[str]


def evaluate(run: Layer2Run, baseline: dict[str, Any] | None) -> Verdict:
    """The gate. `baseline` is the "layer2" block of evals/baseline.json, or None."""
    if run.quota_exhausted:
        return Verdict("quota_exhausted", True, ["provider quota exhausted; run incomplete"])
    schema_rate = run.rate("SchemaError")
    if schema_rate > MAX_ERROR_RATE:
        return Verdict("schema_failures", False, [f"schema failure rate {schema_rate:.2f}"])
    error_rate = run.rate("SchemaError", exclude=True)
    if error_rate > MAX_ERROR_RATE:
        return Verdict("llm_errors", False, [f"provider error rate {error_rate:.2f}"])
    if baseline is None:
        return Verdict("no_baseline", True, ["no layer2 baseline yet: numbers not gated"])
    reasons: list[str] = []
    base_overall = float(baseline["overall"])
    overall = run.overall or 0.0
    if overall + _EPSILON < base_overall - MAX_OVERALL_DROP:
        reasons.append(f"overall: {overall:.3f} < baseline {base_overall:.3f} - 0.02")
    for name, base in baseline.get("fields", {}).items():
        if name not in run.per_field:
            continue
        current = run.per_field[name]
        if current + _EPSILON < float(base) - MAX_FIELD_DROP:
            reasons.append(f"{name}: {current:.3f} < baseline {float(base):.3f} - 0.10")
    if reasons:
        return Verdict("regression", False, reasons)
    return Verdict("ok", True, [])


def build_report(run: Layer2Run, verdict: Verdict, baseline: dict[str, Any] | None) -> Any:
    return {
        "layer": 2,
        "status": verdict.status,
        "passed": verdict.passed,
        "reasons": verdict.reasons,
        "provider": run.provider,
        "model": run.model,
        "prompt_version": PROMPT_VERSION,
        "attempted": run.attempted,
        "errors": run.errors,
        "overall": run.overall,
        "fields": run.per_field,
        "usage": {
            "input_tokens": run.usage.input_tokens,
            "output_tokens": run.usage.output_tokens,
        },
        "baseline": baseline,
        "baseline_candidate": run.baseline_block(),
        "invoices": [
            {
                "invoice_id": r.invoice_id,
                "anomalies": r.anomalies,
                "error": r.error,
                "grounding_issues": r.grounding_issues,
                "correct": sum(r.fields.values()),
                "mismatches": r.mismatches,
            }
            for r in run.results
        ],
    }


def read_baseline_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def write_baseline(path: Path, block: dict[str, Any]) -> None:
    """Set the "layer2" key, keeping every other key (Layer 1's) as it is."""
    data = read_baseline_file(path)
    data["layer2"] = block
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _default_gateway() -> LLMGateway:
    # No database in the eval job: the ledger stays in memory.
    return build_gateway(Settings(), ledger=InMemoryLedger())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.evals.layer2", description=__doc__)
    parser.add_argument("--seed-dir", type=Path, default=DEFAULT_SEED_DIR)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument(
        "--delay", type=float, default=DEFAULT_DELAY_S, help="seconds between calls"
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help='store this run as the "layer2" baseline (only when the run is complete)',
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    gateway_factory: Callable[[], LLMGateway] = _default_gateway,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    args = _parser().parse_args(argv)
    if args.limit < 1:
        print("layer2: --limit must be at least 1", file=sys.stderr)
        return 2
    try:
        gateway = gateway_factory()
    except GatewayConfigError as exc:
        print(
            f"layer2: cannot build the LLM gateway ({exc}); set GEMINI_API_KEY "
            "(or the key of LLM_PROVIDER)",
            file=sys.stderr,
        )
        return 2
    baseline = read_baseline_file(args.baseline).get("layer2")
    ids = select_subset(load_index(args.seed_dir), args.limit)
    run = run_layer2(gateway, args.seed_dir, ids, delay_s=args.delay, sleep=sleep)
    verdict = evaluate(run, baseline)
    report = build_report(run, verdict, baseline)

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))

    if verdict.status == "quota_exhausted":
        print(
            f"layer2: WARNING provider quota exhausted after {run.attempted} invoice(s); "
            "the eval is incomplete and not gated",
            file=sys.stderr,
        )
    complete = verdict.status not in ("quota_exhausted", "schema_failures", "llm_errors")
    if args.write_baseline and complete:
        write_baseline(args.baseline, run.baseline_block())
        print(f"layer2: baseline written to {args.baseline}", file=sys.stderr)
        return 0
    if not verdict.passed:
        print("layer2: FAILED: " + "; ".join(verdict.reasons), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
