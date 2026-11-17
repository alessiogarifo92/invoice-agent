"""Layer-1 eval: rules and matching on the seed's ground-truth fields, $0 and no database.

For every seed invoice, in issue order (so a resend is checked against the invoices before
it): the facts come from the ground-truth `fields`, the hidden-text findings and the regex
sweep from the real PDF, the context from the in-memory matcher over master.json. The final
action is the rule floor; the agent (Layer 3) can only raise it.

Hard failures (exit 1): a fraud or injection case approved, or overall recall below the
committed baseline. `--write-baseline` records this run under the "layer1" key.

Usage (from backend/):
    uv run python -m app.evals.layer1 --seed-dir ../data/seed --baseline ../evals/baseline.json
"""

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.ingestion import extract_document
from app.matching.context import build_context, document_signals
from app.matching.memory import MemoryMatcher
from app.rules.facts import parse_facts
from app.rules.flags import ACTION_ORDER, Action, rule_floor
from app.rules.runner import run_rules

# A flag that detects the anomaly: its code, and for injections the finding kind it must name.
FlagKey = tuple[str, str | None]

FAMILIES: dict[str, frozenset[FlagKey]] = {
    "iban_swap": frozenset({("iban_not_current", None)}),
    "resend_reformatted_number": frozenset(
        {("duplicate_confirmed", None), ("duplicate_suspected", None)}
    ),
    "amount_above_tolerance": frozenset(
        {("amount_above_tolerance", None), ("po_balance_exceeded", None)}
    ),
    "unknown_vendor": frozenset({("unknown_vendor", None)}),
    "wrong_vat": frozenset({("wrong_vat", None)}),
    "total_mismatch": frozenset({("total_mismatch", None)}),
    "price_above_contract": frozenset({("price_above_contract", None)}),
    "injection_visible_note": frozenset({("injection_suspected", "instruction_like_text")}),
    "injection_white_text": frozenset({("injection_suspected", "white_text")}),
    "injection_tiny_font": frozenset({("injection_suspected", "tiny_font")}),
    "injection_metadata": frozenset({("injection_suspected", "metadata_instruction")}),
}

_INJECTIONS = [name for name in FAMILIES if name.startswith("injection_")]

# Other flags an anomaly correctly causes: they count as right for precision, not for recall.
SIDE_EFFECTS: dict[str, frozenset[str]] = {
    # The resend bills its PO a second time.
    "resend_reformatted_number": frozenset({"po_balance_exceeded"}),
    # The invented vendor quotes a PO that does not exist.
    "unknown_vendor": frozenset({"po_not_found"}),
    # Every injection payload names the attacker's IBAN.
    **{name: frozenset({"iban_in_text_not_current"}) for name in _INJECTIONS},
}

# Approving any of these is a hard failure, whatever else the run shows.
NEVER_APPROVE = frozenset(
    {"iban_swap", "resend_reformatted_number", "unknown_vendor", *_INJECTIONS}
)

RECALL_DIGITS = 4


@dataclass(frozen=True)
class InvoiceResult:
    invoice_id: str
    anomalies: tuple[str, ...]
    expected_action: Action
    action: Action
    flags: tuple[FlagKey, ...]  # (code, injection kind or None), in rule order

    def detects(self, anomaly: str) -> bool:
        family = FAMILIES.get(anomaly, frozenset())
        hit = any((code, None) in family or (code, kind) in family for code, kind in self.flags)
        return hit and ACTION_ORDER[self.action] >= ACTION_ORDER[self.expected_action]

    def explains(self, code: str) -> bool:
        return any(
            code in {c for c, _ in FAMILIES.get(a, frozenset())} | SIDE_EFFECTS.get(a, frozenset())
            for a in self.anomalies
        )


@dataclass(frozen=True)
class Recall:
    cases: int
    detected: int

    @property
    def recall(self) -> float:
        return self.detected / self.cases if self.cases else 1.0


@dataclass(frozen=True)
class Precision:
    fired: int  # invoices on which the code was raised
    explained: int  # ... that carry an anomaly this code detects or causes

    @property
    def precision(self) -> float:
        return self.explained / self.fired if self.fired else 1.0


@dataclass(frozen=True)
class Report:
    results: tuple[InvoiceResult, ...]
    recall: dict[str, Recall]
    precision: dict[str, Precision]
    confusion: dict[str, dict[str, int]]  # expected action -> final action -> invoices
    clean_approve_rate: float
    overall_recall: float
    hard_failures: list[str]

    @property
    def invoices(self) -> int:
        return len(self.results)

    def baseline(self) -> dict[str, Any]:
        def r(value: float) -> float:
            return round(value, RECALL_DIGITS)

        return {
            "invoices": self.invoices,
            "overall_recall": r(self.overall_recall),
            "clean_approve_rate": r(self.clean_approve_rate),
            "recall": {name: r(rec.recall) for name, rec in self.recall.items()},
            "precision": {code: r(p.precision) for code, p in self.precision.items()},
            "confusion": self.confusion,
        }


def score(results: Iterable[InvoiceResult]) -> Report:
    results = tuple(results)
    recall: dict[str, Recall] = {}
    for anomaly in sorted({a for r in results for a in r.anomalies}):
        cases = [r for r in results if anomaly in r.anomalies]
        recall[anomaly] = Recall(len(cases), sum(r.detects(anomaly) for r in cases))

    precision: dict[str, Precision] = {}
    for code in sorted({c for r in results for c, _ in r.flags}):
        fired = [r for r in results if any(c == code for c, _ in r.flags)]
        precision[code] = Precision(len(fired), sum(r.explains(code) for r in fired))

    confusion: dict[str, dict[str, int]] = {}
    for (expected, action), n in sorted(
        Counter((r.expected_action, r.action) for r in results).items()
    ):
        confusion.setdefault(expected, {})[action] = n

    clean = [r for r in results if not r.anomalies]
    clean_rate = sum(r.action == "approve" for r in clean) / len(clean) if clean else 1.0
    total_cases = sum(rec.cases for rec in recall.values())
    detected = sum(rec.detected for rec in recall.values())
    overall = detected / total_cases if total_cases else 1.0
    hard = [
        f"{r.invoice_id} ({', '.join(sorted(NEVER_APPROVE & set(r.anomalies)))}) was approved"
        for r in results
        if r.action == "approve" and NEVER_APPROVE & set(r.anomalies)
    ]
    return Report(results, recall, precision, confusion, clean_rate, overall, hard)


def check(report: Report, baseline: Mapping[str, Any] | None) -> list[str]:
    """Every hard failure: approved fraud or injection cases, and recall below the baseline."""
    failures = list(report.hard_failures)
    if baseline is not None:
        floor = float(baseline["overall_recall"])
        if round(report.overall_recall, RECALL_DIGITS) < floor:
            failures.append(
                f"overall recall {report.overall_recall:.4f} is below the baseline {floor:.4f}"
            )
    return failures


def _seed_records(seed_dir: Path) -> list[dict[str, Any]]:
    index = json.loads((seed_dir / "index.json").read_text(encoding="utf-8"))
    records = [
        json.loads((seed_dir / "invoices" / f"{entry['invoice_id']}.json").read_text("utf-8"))
        for entry in index["invoices"]
    ]
    return sorted(records, key=lambda r: (r["fields"]["issue_date"], r["invoice_id"]))


def evaluate(seed_dir: Path) -> Report:
    matcher = MemoryMatcher.from_master_file(seed_dir / "master.json")
    results = []
    for record in _seed_records(seed_dir):
        invoice_id = record["invoice_id"]
        doc = extract_document((seed_dir / "invoices" / record["file"]).read_bytes())
        findings, sweep = document_signals(doc)
        facts = parse_facts(record["fields"])
        flags = run_rules(facts, build_context(matcher, facts, invoice_id, findings, sweep))
        results.append(
            InvoiceResult(
                invoice_id=invoice_id,
                anomalies=tuple(record["anomalies"]),
                expected_action=record["expected_action"],
                action=rule_floor(flags),
                flags=tuple(
                    (f.code, f.evidence.get("kind") if f.code == "injection_suspected" else None)
                    for f in flags
                ),
            )
        )
        matcher.add_extraction(invoice_id, record["fields"])
    return score(results)


def render(report: Report) -> str:
    lines = [f"Layer 1: {report.invoices} invoices", "", "Recall per anomaly"]
    for name, rec in report.recall.items():
        lines.append(f"  {name:<28} {rec.detected:>3}/{rec.cases:<3} {rec.recall:>7.2%}")
    lines += ["", "Precision per flag code (invoices flagged / explained by an anomaly)"]
    for code, p in report.precision.items():
        lines.append(f"  {code:<28} {p.explained:>3}/{p.fired:<3} {p.precision:>7.2%}")
    actions = list(ACTION_ORDER)
    lines += ["", "Action confusion (rows: expected, columns: final)"]
    lines.append("  " + " " * 10 + "".join(f"{a:>9}" for a in actions))
    for expected in actions:
        row = report.confusion.get(expected, {})
        lines.append(f"  {expected:<10}" + "".join(f"{row.get(a, 0):>9}" for a in actions))
    lines += [
        "",
        f"overall recall      {report.overall_recall:.2%}",
        f"clean approve rate  {report.clean_approve_rate:.2%}",
    ]
    flagged_clean = [r for r in report.results if not r.anomalies and r.action != "approve"]
    for r in flagged_clean:
        lines.append(f"  clean but {r.action}: {r.invoice_id} {[c for c, _ in r.flags]}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.evals.layer1", description=__doc__)
    parser.add_argument("--seed-dir", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument(
        "--write-baseline", action="store_true", help="record this run as the layer1 baseline"
    )
    args = parser.parse_args(argv)

    stored: dict[str, Any] = {}
    if args.baseline.exists():
        stored = json.loads(args.baseline.read_text(encoding="utf-8"))
    elif not args.write_baseline:
        print(f"{args.baseline}: no baseline; run once with --write-baseline", file=sys.stderr)
        return 2

    if not args.write_baseline and "layer1" not in stored:
        print(
            f"{args.baseline}: no layer1 baseline; run once with --write-baseline", file=sys.stderr
        )
        return 2

    report = evaluate(args.seed_dir)
    print(render(report))
    failures = check(report, None if args.write_baseline else stored.get("layer1"))
    if failures:
        print("\nFAIL", *failures, sep="\n  ", file=sys.stderr)
        return 1
    if args.write_baseline:
        stored["layer1"] = report.baseline()
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")
        print(f"\nbaseline written to {args.baseline}")
    else:
        print("\nPASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
