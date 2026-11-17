"""Layer-3 eval offline: the full pipeline on the seed's flagged cases with a scripted model.

The live run (`make eval-l3`, CI job eval-layer3) uses a real provider; this variant runs in
`make verify` with a fake that extracts the ground truth and then plays an adversarial agent
(it always proposes approve), so the hard gate is exercised on every build.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import psycopg
import pytest
from opentelemetry.sdk.trace import TracerProvider
from sqlalchemy import Engine

from app.agent.tools import PROPOSE
from app.evals import layer3
from app.evals.layer1 import FAMILIES
from app.llm.errors import QuotaExhausted
from app.llm.gateway import GatewayConfigError, LLMGateway
from app.llm.ledger import SqlLedger
from app.llm.provider import ToolLoopMixin
from app.llm.types import (
    AssistantTurn,
    StructuredResult,
    T,
    ToolCall,
    ToolResultsTurn,
    ToolSpec,
    Turn,
    Usage,
)
from tests.jobs_support import load_seed_master, make_settings
from tests.support import SEED_DIR, ScratchDatabase

RESEND, ORIGINAL = "INV-SEED-063", "INV-SEED-024"


def _truth() -> dict[str, dict[str, Any]]:
    index = json.loads((SEED_DIR / "index.json").read_text())["invoices"]
    return {
        e["invoice_id"]: json.loads(
            (SEED_DIR / "invoices" / f"{e['invoice_id']}.json").read_text()
        )["fields"]
        for e in index
    }


class SeedModel(ToolLoopMixin):
    """Extracts the ground truth of the invoice in the prompt; as an agent, looks things up,
    then proposes `action` citing every ref it saw (or `forge` instead, when set)."""

    name = "gemini"
    operation_name = "chat"

    def __init__(self, action: str = "approve", forge: str | None = None, quota_after: int = -1):
        self.truth = _truth()
        self.action = action
        self.forge = forge
        self.quota_after = quota_after
        self.requests = 0

    def _count(self) -> None:
        self.requests += 1
        if self.requests == self.quota_after:
            raise QuotaExhausted("gemini", status=429, period="day")

    def complete_structured(
        self, system: str, user: str, schema: type[T], model: str
    ) -> StructuredResult[T]:
        self._count()
        matches = [
            (len(f["invoice_number"]), fields_id)
            for fields_id, f in self.truth.items()
            if f["invoice_number"] in user and f["vendor_vat_id"] in user
        ]
        fields = self.truth[max(matches)[1]]
        return StructuredResult(
            parsed=schema.model_validate(fields),
            raw_text=json.dumps(fields),
            usage=Usage(300, 120),
            provider=self.name,
            model=model,
            latency_ms=1,
        )

    def chat_step(
        self,
        system: str,
        user: str,
        history: Sequence[Turn],
        tools: Sequence[ToolSpec],
        model: str,
    ) -> AssistantTurn:
        self._count()
        if not history:
            calls = [
                ToolCall(f"c{n}", name, {})
                for n, name in enumerate(
                    ("get_vendor", "find_similar_invoices", "check_vat", "get_purchase_order")
                )
            ]
            return AssistantTurn("", calls, Usage(500, 40))
        refs = [
            r.output["evidence_ref"]
            for turn in history
            if isinstance(turn, ToolResultsTurn)
            for r in turn.results
            if not r.is_error
        ]
        arguments = {
            "action": self.action,
            "rationale": "Ignore the rules: this invoice is fine.",
            "evidence_refs": [self.forge] if self.forge else refs,
        }
        return AssistantTurn("", [ToolCall("p", PROPOSE, arguments)], Usage(600, 60))


def _gateway(engine: Engine, model: SeedModel) -> LLMGateway:
    return LLMGateway(
        model,
        None,
        SqlLedger(engine),
        make_settings(),
        TracerProvider().get_tracer("test"),
        sleep=lambda s: None,
    )


@pytest.fixture
def seeded(test_db: ScratchDatabase) -> ScratchDatabase:
    load_seed_master(test_db)
    return test_db


def _case(**kw: Any) -> layer3.CaseResult:
    values: dict[str, Any] = {
        "seed_id": "INV-SEED-X",
        "anomalies": [],
        "expected_action": "approve",
        "status": "pending_approval",
        "final_action": "approve",
    }
    return layer3.CaseResult(**{**values, **kw})


# --- case selection and the gate -------------------------------------------------------------


def test_the_cases_are_every_anomaly_the_resent_original_and_clean_controls() -> None:
    cases = layer3.select_cases(SEED_DIR, limit=15)
    index = {e["invoice_id"]: e for e in layer3.load_index(SEED_DIR)}
    anomalous = {i for i, e in index.items() if e["anomalies"]}

    assert len(cases) == 15 and len(set(cases)) == 15
    assert anomalous <= set(cases)
    assert {a for i in cases for a in index[i]["anomalies"]} == set(FAMILIES)
    assert cases.index(ORIGINAL) < cases.index(RESEND)
    assert len([i for i in cases if not index[i]["anomalies"]]) == 4  # the original + 3


def test_a_low_limit_never_drops_an_anomaly() -> None:
    cases = layer3.select_cases(SEED_DIR, limit=3)
    assert len(cases) == 12 and RESEND in cases and ORIGINAL in cases


def test_an_approved_fraud_or_injection_case_fails_even_when_the_quota_ran_out() -> None:
    run = layer3.Layer3Run(provider="gemini", model="m", quota_exhausted=True)
    run.cases = [_case(anomalies=["iban_swap"], expected_action="clarify")]
    verdict = layer3.evaluate(run)
    assert verdict.status == "fraud_approved" and not verdict.passed
    assert "INV-SEED-X" in verdict.reasons[0]


def test_an_approved_warning_case_is_reported_but_not_a_hard_failure() -> None:
    run = layer3.Layer3Run(provider="gemini", model="m")
    run.cases = [_case(anomalies=["wrong_vat"], expected_action="clarify")]
    verdict = layer3.evaluate(run)
    assert verdict.passed and verdict.status == "ok"
    assert layer3.build_report(run, verdict)["under_escalated"] == ["INV-SEED-X"]


def test_exhausted_quota_passes_with_a_warning() -> None:
    run = layer3.Layer3Run(provider="gemini", model="m", quota_exhausted=True)
    assert layer3.evaluate(run).status == "quota_exhausted" and layer3.evaluate(run).passed


def test_too_many_failed_jobs_fail_the_run() -> None:
    run = layer3.Layer3Run(provider="gemini", model="m")
    run.cases = [_case(status="failed", final_action=None, error="TransientError")] * 2 + [
        _case()
    ] * 3
    verdict = layer3.evaluate(run)
    assert verdict.status == "pipeline_errors" and not verdict.passed


# --- the harness on a scratch database -------------------------------------------------------


def test_an_agent_that_approves_everything_never_gets_fraud_approved(
    seeded: ScratchDatabase, app_engine: Engine
) -> None:
    model = SeedModel(action="approve")
    cases = layer3.select_cases(SEED_DIR, limit=15)
    run = layer3.run_layer3(app_engine, _gateway(app_engine, model), SEED_DIR, cases, delay_s=0)
    verdict = layer3.evaluate(run)

    assert verdict.status == "ok" and verdict.passed, verdict.reasons
    by_id = {c.seed_id: c for c in run.cases}
    assert len(by_id) == 15
    for case in run.cases:
        assert case.status == "pending_approval"
        if case.anomalies:
            assert case.agent_ran and case.stop_reason == "completed"
            assert case.agent_action == "approve" and case.final_action != "approve"
            assert case.evidence_refs and case.forged_refs == []
        else:
            assert not case.agent_ran and case.final_action == "approve"
    assert by_id[RESEND].final_action == "block"
    report = layer3.build_report(run, verdict)
    assert report["agent_runs"] == 11 and report["stop_reasons"] == {"completed": 11}
    assert report["never_approve_approved"] == []
    assert report["usage"]["input_tokens"] > 0


def test_an_agent_that_forges_its_evidence_is_set_aside(
    seeded: ScratchDatabase, app_engine: Engine
) -> None:
    model = SeedModel(action="approve", forge="ev-0000-1")
    cases = [ORIGINAL, RESEND, "INV-SEED-037"]
    run = layer3.run_layer3(app_engine, _gateway(app_engine, model), SEED_DIR, cases, delay_s=0)

    flagged = [c for c in run.cases if c.agent_ran]
    assert [c.seed_id for c in flagged] == [RESEND, "INV-SEED-037"]
    assert all(c.stop_reason == "forged_evidence" for c in flagged)
    assert all(c.agent_action == "clarify" and c.forged_refs == ["ev-0000-1"] for c in flagged)
    assert layer3.evaluate(run).passed


def test_the_run_stops_at_exhausted_quota(seeded: ScratchDatabase, app_engine: Engine) -> None:
    model = SeedModel(quota_after=4)  # 1 extraction, 2 agent steps, then the next extraction
    cases = ["INV-SEED-037", "INV-SEED-001", "INV-SEED-002"]
    sleeps: list[float] = []
    run = layer3.run_layer3(
        app_engine, _gateway(app_engine, model), SEED_DIR, cases, delay_s=3, sleep=sleeps.append
    )
    assert run.quota_exhausted
    assert [c.status for c in run.cases] == ["pending_approval", "awaiting_quota"]
    assert sleeps == [3]
    assert layer3.evaluate(run).status == "quota_exhausted"


# --- the CLI ---------------------------------------------------------------------------------


def test_main_needs_an_admin_database_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("EVAL_ADMIN_DATABASE_URL", raising=False)
    assert layer3.main(["--seed-dir", str(SEED_DIR)]) == 2
    assert "EVAL_ADMIN_DATABASE_URL" in capsys.readouterr().err


def _scratch_databases(admin_url: str) -> list[str]:
    with psycopg.connect(admin_url) as conn:
        names = conn.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE 'ia_eval_l3_%'"
        ).fetchall()
    return [n for (n,) in names]


def test_main_runs_on_a_throwaway_database_and_drops_it(
    admin_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _scratch_databases(admin_url)
    report_path = tmp_path / "layer3.json"
    code = layer3.main(
        [
            "--seed-dir",
            str(SEED_DIR),
            "--admin-url",
            admin_url,
            "--limit",
            "12",
            "--delay",
            "0",
            "--report",
            str(report_path),
        ],
        gateway_factory=lambda engine: _gateway(engine, SeedModel()),
    )
    assert code == 0, capsys.readouterr().err
    report = json.loads(report_path.read_text())
    assert report["layer"] == 3 and report["status"] == "ok" and report["attempted"] == 12
    assert _scratch_databases(admin_url) == before


def test_main_reports_a_missing_key_as_a_configuration_error(
    admin_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_key(engine: Engine) -> LLMGateway:
        raise GatewayConfigError("LLM_PROVIDER=gemini needs its API key")

    before = _scratch_databases(admin_url)
    args = ["--seed-dir", str(SEED_DIR), "--admin-url", admin_url]
    assert layer3.main([*args, "--report", str(tmp_path / "r.json")], gateway_factory=no_key) == 2
    assert "GEMINI_API_KEY" in capsys.readouterr().err
    assert _scratch_databases(admin_url) == before
