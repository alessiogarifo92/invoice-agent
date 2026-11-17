"""Layer-1 eval: rules and matching on the seed's ground-truth fields and real PDFs."""

import json
from pathlib import Path

import pytest

from app.evals import layer1
from app.evals.layer1 import InvoiceResult, check, evaluate, score
from app.rules import RULES, Flag, InvoiceFacts, RuleContext, run_rules
from tests.support import SEED_DIR


@pytest.fixture(scope="module")
def seed_report() -> layer1.Report:
    return evaluate(SEED_DIR)


def test_seed_meets_every_hard_gate(seed_report: layer1.Report) -> None:
    assert seed_report.invoices == 63
    assert seed_report.hard_failures == []
    assert seed_report.overall_recall == 1.0
    assert seed_report.clean_approve_rate == 1.0


def test_seed_actions(seed_report: layer1.Report) -> None:
    by_id = {r.invoice_id: r for r in seed_report.results}
    assert by_id["INV-SEED-063"].action == "block"  # the reformatted resend of INV-SEED-024
    assert by_id["INV-SEED-024"].action == "approve"  # the original came first
    assert seed_report.confusion["approve"] == {"approve": 52}
    assert seed_report.confusion["block"] == {"block": 1}
    assert seed_report.confusion["clarify"] == {"clarify": 10}


def test_seed_recall_per_anomaly_and_known_extra_flags(seed_report: layer1.Report) -> None:
    assert set(seed_report.recall) == set(layer1.FAMILIES)
    assert all(r.recall == 1.0 for r in seed_report.recall.values())
    # The attacker IBAN in every injection payload is caught by the text sweep as well.
    assert seed_report.precision["iban_in_text_not_current"].fired == 4
    assert all(p.precision == 1.0 for p in seed_report.precision.values())


def _result(anomalies: tuple[str, ...], expected: str, action: str, *codes: str) -> InvoiceResult:
    return InvoiceResult(
        invoice_id="X",
        anomalies=anomalies,
        expected_action=expected,  # type: ignore[arg-type]
        action=action,  # type: ignore[arg-type]
        flags=tuple((code, None) for code in codes),
    )


def test_an_approved_fraud_case_is_a_hard_failure() -> None:
    report = score([_result(("iban_swap",), "clarify", "approve")])
    assert report.hard_failures == ["X (iban_swap) was approved"]
    assert report.recall["iban_swap"].recall == 0.0
    assert check(report, None) == ["X (iban_swap) was approved"]


def test_an_approved_injection_case_is_a_hard_failure() -> None:
    report = score([_result(("injection_tiny_font",), "clarify", "approve")])
    assert report.hard_failures == ["X (injection_tiny_font) was approved"]


def test_detection_needs_the_family_flag_and_the_action() -> None:
    # Right flag, too weak an action: the resend must block.
    weak = score(
        [_result(("resend_reformatted_number",), "block", "clarify", "duplicate_suspected")]
    )
    assert weak.recall["resend_reformatted_number"].detected == 0
    # Right action from an unrelated flag: not a detection either.
    unrelated = score([_result(("wrong_vat",), "clarify", "clarify", "total_mismatch")])
    assert unrelated.recall["wrong_vat"].detected == 0
    assert unrelated.precision["total_mismatch"].precision == 0.0


def test_injection_detection_needs_the_matching_finding_kind() -> None:
    result = InvoiceResult(
        "X",
        ("injection_white_text",),
        "clarify",
        "clarify",
        (("injection_suspected", "tiny_font"),),
    )
    assert score([result]).recall["injection_white_text"].detected == 0


def test_a_flagged_clean_invoice_lowers_the_clean_approve_rate() -> None:
    report = score(
        [_result((), "approve", "clarify", "missing_field"), _result((), "approve", "approve")]
    )
    assert report.clean_approve_rate == 0.5
    assert report.precision["missing_field"].precision == 0.0
    assert report.hard_failures == []


def test_recall_below_the_baseline_fails() -> None:
    report = score([_result(("wrong_vat",), "clarify", "clarify", "wrong_vat")])
    assert check(report, {"overall_recall": 1.0}) == []
    missed = score([_result(("wrong_vat",), "clarify", "approve")])
    assert check(missed, {"overall_recall": 1.0}) == [
        "overall recall 0.0000 is below the baseline 1.0000"
    ]


def test_cli_writes_then_checks_the_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"layer2": {"keep": True}}))
    args = ["--seed-dir", str(SEED_DIR), "--baseline", str(baseline)]
    assert layer1.main([*args, "--write-baseline"]) == 0
    written = json.loads(baseline.read_text())
    assert written["layer2"] == {"keep": True}
    assert written["layer1"]["overall_recall"] == 1.0
    assert written["layer1"]["clean_approve_rate"] == 1.0
    assert layer1.main(args) == 0
    out = capsys.readouterr().out
    assert "overall recall" in out and "iban_swap" in out

    written["layer1"]["overall_recall"] = 1.5
    baseline.write_text(json.dumps(written))
    assert layer1.main(args) == 1


def test_cli_without_a_baseline_file_is_a_usage_error(tmp_path: Path) -> None:
    args = ["--seed-dir", str(SEED_DIR), "--baseline", str(tmp_path / "missing.json")]
    assert layer1.main(args) == 2


def test_cli_with_a_baseline_lacking_layer1_is_a_usage_error(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"layer2": {}}))
    assert layer1.main(["--seed-dir", str(SEED_DIR), "--baseline", str(baseline)]) == 2


def test_the_gate_trips_on_the_seed_when_a_rule_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    without_iban = tuple((n, r) for n, r in RULES if n != "iban")

    def run_without_iban(facts: InvoiceFacts, ctx: RuleContext) -> list[Flag]:
        return run_rules(facts, ctx, rules=without_iban)

    monkeypatch.setattr(layer1, "run_rules", run_without_iban)
    report = evaluate(SEED_DIR)
    assert report.hard_failures == ["INV-SEED-037 (iban_swap) was approved"]
    assert check(report, {"overall_recall": 1.0})[1].startswith("overall recall 0.9091")
