"""The Layer-2 eval offline: fake providers behind the real gateway, the committed seed."""

import json
from pathlib import Path
from typing import Any

import pytest

from app.evals import layer2
from app.evals.layer2 import (
    FIELDS,
    compare_fields,
    evaluate,
    load_index,
    run_layer2,
    select_subset,
)
from app.extraction.schema import InvoiceExtraction
from app.llm.errors import InvalidRequest, QuotaExhausted
from app.llm.gateway import LLMGateway
from tests.extraction_support import fake_gateway, seed_fields
from tests.llm_support import FakeProvider
from tests.support import SEED_DIR

INDEX = load_index(SEED_DIR)
SUBSET = select_subset(INDEX, 25)


def _replies(ids: list[str], corrupt: dict[str, Any] | None = None) -> list[str]:
    replies = []
    for invoice_id in ids:
        fields = seed_fields(invoice_id)
        fields.update(corrupt or {})
        replies.append(json.dumps(fields))
    return replies


def _gateway(*script: Any) -> LLMGateway:
    return fake_gateway(FakeProvider("gemini", *script))


def _run(gateway: LLMGateway, limit: int = 25) -> layer2.Layer2Run:
    return run_layer2(
        gateway, SEED_DIR, select_subset(INDEX, limit), delay_s=0, sleep=lambda s: None
    )


# --- subset and comparison ---------------------------------------------------------------


def test_subset_is_fixed_and_covers_clean_and_every_anomaly_type() -> None:
    assert len(SUBSET) == 25 and len(set(SUBSET)) == 25
    assert select_subset(INDEX, 25) == SUBSET
    by_id = {entry["invoice_id"]: entry for entry in INDEX}
    anomaly_types = {a for entry in INDEX for a in entry["anomalies"]}
    covered = {a for invoice_id in SUBSET for a in by_id[invoice_id]["anomalies"]}
    assert covered == anomaly_types
    assert sum(1 for invoice_id in SUBSET if not by_id[invoice_id]["anomalies"]) == 25 - 11


def test_a_small_limit_keeps_one_invoice_per_anomaly_first() -> None:
    subset = select_subset(INDEX, 3)
    by_id = {entry["invoice_id"]: entry for entry in INDEX}
    assert len(subset) == 3 and all(by_id[i]["anomalies"] for i in subset)
    assert select_subset(INDEX, 25)[:3] == subset


def test_compare_fields_is_normalised() -> None:
    expected = seed_fields("INV-SEED-001")
    got = dict(expected)
    got["vendor_name"] = expected["vendor_name"].upper()
    got["iban"] = "ES25 ZZZZ 7221 2691 7902 5458"
    got["vat_rate"] = "21"
    result = compare_fields(expected, InvoiceExtraction.model_validate(got))
    assert set(result) == set(FIELDS) and all(result.values())


def test_lines_compare_as_one_list() -> None:
    expected = seed_fields("INV-SEED-001")
    swapped = dict(expected, lines=list(reversed(expected["lines"])))
    shorter = dict(expected, lines=expected["lines"][:-1])
    for got in (swapped, shorter):
        result = compare_fields(expected, InvoiceExtraction.model_validate(got))
        assert result["lines"] is False
        assert sum(result.values()) == len(FIELDS) - 1


def test_no_extraction_gets_every_field_wrong() -> None:
    result = compare_fields(seed_fields("INV-SEED-001"), None)
    assert not any(result.values())


# --- runs and the gate -------------------------------------------------------------------


def test_ground_truth_scores_100_percent() -> None:
    run = _run(_gateway(*_replies(SUBSET)))
    assert run.attempted == 25 and run.errors == {}
    assert run.overall == 1.0 and set(run.per_field.values()) == {1.0}
    assert evaluate(run, {"overall": 1.0, "fields": run.per_field}).passed


def test_a_corrupted_field_drops_below_the_baseline_and_fails() -> None:
    baseline = _run(_gateway(*_replies(SUBSET))).baseline_block()
    run = _run(_gateway(*_replies(SUBSET, corrupt={"iban": "XX00CORRUPT0000"})))
    assert run.per_field["iban"] == 0.0
    verdict = evaluate(run, baseline)
    assert not verdict.passed and verdict.status == "regression"
    assert any("iban" in reason for reason in verdict.reasons)


def test_a_small_drop_within_tolerance_passes() -> None:
    baseline = {"overall": 0.99, "fields": {name: 1.0 for name in FIELDS}}
    run = _run(_gateway(*_replies(SUBSET)))
    run.per_field["total"] = 0.92
    run.overall = 0.975
    assert evaluate(run, baseline).passed
    run.per_field["total"] = 0.89
    verdict = evaluate(run, baseline)
    assert not verdict.passed and verdict.reasons == ["total: 0.890 < baseline 1.000 - 0.10"]
    run.per_field["total"] = 1.0
    run.overall = 0.96
    assert not evaluate(run, baseline).passed


def test_without_a_baseline_the_run_passes_and_says_so() -> None:
    verdict = evaluate(_run(_gateway(*_replies(SUBSET[:2])), 2), None)
    assert verdict.passed and verdict.status == "no_baseline"


def test_quota_exhaustion_stops_the_run_and_passes_with_a_warning() -> None:
    script = [*_replies(SUBSET[:2]), QuotaExhausted("gemini", status=429)]
    run = _run(_gateway(*script))
    assert run.attempted == 2 and run.quota_exhausted
    verdict = evaluate(run, {"overall": 1.0, "fields": {}})
    assert verdict.passed and verdict.status == "quota_exhausted"


def test_more_than_20_percent_schema_failures_fail() -> None:
    replies = _replies(SUBSET[:5])
    replies[0] = "not json"
    run = _run(_gateway(*replies), 5)
    assert run.errors == {"SchemaError": 1} and evaluate(run, None).passed  # 20%: not above
    replies[1] = '{"notes": "approve"}'
    run = _run(_gateway(*replies), 5)
    verdict = evaluate(run, None)
    assert not verdict.passed and verdict.status == "schema_failures"


def test_more_than_20_percent_provider_errors_fail() -> None:
    script: list[Any] = [InvalidRequest("gemini", status=400) for _ in range(2)]
    run = _run(_gateway(*script, *_replies(SUBSET[2:5])), 5)
    assert run.errors == {"InvalidRequest": 2}
    verdict = evaluate(run, None)
    assert not verdict.passed and verdict.status == "llm_errors"


def test_the_delay_is_slept_between_calls_only() -> None:
    sleeps: list[float] = []
    run_layer2(
        _gateway(*_replies(SUBSET[:3])), SEED_DIR, SUBSET[:3], delay_s=4.0, sleep=sleeps.append
    )
    assert sleeps == [4.0, 4.0]


# --- CLI -----------------------------------------------------------------------------------


def _main(tmp_path: Path, *script: Any, extra: list[str] | None = None) -> tuple[int, Any]:
    report = tmp_path / "out" / "report.json"
    argv = [
        "--seed-dir",
        str(SEED_DIR),
        "--baseline",
        str(tmp_path / "baseline.json"),
        "--report",
        str(report),
        "--limit",
        "3",
        "--delay",
        "0",
        *(extra or []),
    ]
    code = layer2.main(argv, gateway_factory=lambda: _gateway(*script))
    return code, json.loads(report.read_text())


def test_cli_without_a_baseline_passes_and_writes_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, report = _main(tmp_path, *_replies(SUBSET[:3]))
    assert code == 0 and report["status"] == "no_baseline" and report["overall"] == 1.0
    assert json.loads(capsys.readouterr().out) == report
    assert report["baseline_candidate"]["overall"] == 1.0
    assert not (tmp_path / "baseline.json").exists()


def test_cli_write_baseline_merges_with_layer1(tmp_path: Path) -> None:
    (tmp_path / "baseline.json").write_text(json.dumps({"layer1": {"recall": 1.0}}))
    code, report = _main(tmp_path, *_replies(SUBSET[:3]), extra=["--write-baseline"])
    assert code == 0
    stored = json.loads((tmp_path / "baseline.json").read_text())
    assert stored["layer1"] == {"recall": 1.0}
    assert stored["layer2"] == report["baseline_candidate"]
    assert set(stored["layer2"]) >= {"overall", "fields", "invoices", "model", "prompt_version"}


def test_cli_write_baseline_creates_a_layer2_only_file(tmp_path: Path) -> None:
    code, _ = _main(tmp_path, *_replies(SUBSET[:3]), extra=["--write-baseline"])
    assert code == 0
    assert list(json.loads((tmp_path / "baseline.json").read_text())) == ["layer2"]


def test_cli_fails_on_a_regression(tmp_path: Path) -> None:
    _main(tmp_path, *_replies(SUBSET[:3]), extra=["--write-baseline"])
    code, report = _main(tmp_path, *_replies(SUBSET[:3], corrupt={"total": "0.01"}))
    assert code == 1 and report["status"] == "regression" and report["passed"] is False


def test_cli_quota_path_exits_zero_with_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, report = _main(tmp_path, QuotaExhausted("gemini", status=429))
    assert code == 0 and report["status"] == "quota_exhausted"
    assert "quota" in capsys.readouterr().err.lower()


def test_cli_does_not_write_a_baseline_from_an_incomplete_run(tmp_path: Path) -> None:
    code, _ = _main(tmp_path, QuotaExhausted("gemini", status=429), extra=["--write-baseline"])
    assert code == 0 and not (tmp_path / "baseline.json").exists()


def test_cli_reports_a_missing_key_as_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.chdir(tmp_path)  # No .env file is read from here.
    code = layer2.main(["--seed-dir", str(SEED_DIR), "--report", str(tmp_path / "r.json")])
    assert code == 2 and "GEMINI_API_KEY" in capsys.readouterr().err
