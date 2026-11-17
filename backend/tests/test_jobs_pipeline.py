"""The worker running the pipeline end to end on a real database, with a fake LLM provider.

extract -> grounding -> matching -> rules -> agent (T7 stub) -> policy gate -> proposal ->
pending_approval. The gateway writes the real ledger, so "never pays twice" is asserted on
llm_calls. The provider is a FakeProvider: no test reaches a live API.
"""

import json
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, text

from app.agent import AgentDecision, AgentRequest
from app.audit import verify_chain
from app.jobs import pipeline, queue
from app.jobs.worker import Worker, quota_retry_at
from app.llm.errors import QuotaExhausted, TransientError
from app.llm.gateway import LLMGateway
from app.rules.flags import Action
from tests.extraction_support import seed_fields
from tests.jobs_support import (
    event_types,
    llm_calls,
    load_seed_master,
    rows,
    scalar,
    sql_gateway,
    upload_seed,
)
from tests.llm_support import FakeProvider
from tests.support import ScratchDatabase

CLEAN = "INV-SEED-001"
IBAN_SWAP = "INV-SEED-037"

Agent = Callable[[AgentRequest], AgentDecision | None]


class Crash(BaseException):
    """Stands in for the process dying: not an Exception, so no handler cleans up after it."""


@pytest.fixture
def seeded(test_db: ScratchDatabase) -> ScratchDatabase:
    load_seed_master(test_db)
    return test_db


def _reply(seed_id: str, **changes: Any) -> str:
    return json.dumps({**seed_fields(seed_id), **changes})


def _worker(
    engine: Engine,
    gateway: LLMGateway | FakeProvider,
    owner: str = "w1",
    agent: Agent | None = None,
    **options: Any,
) -> Worker:
    if isinstance(gateway, FakeProvider):
        gateway = sql_gateway(engine, gateway)
    options.setdefault("retry_backoff_s", 0.0)
    if agent is not None:
        options["agent"] = agent
    return Worker(engine, gateway, owner=owner, **options)


def _job(engine: Engine, invoice_id: UUID) -> Any:
    (job,) = rows(
        engine,
        "SELECT * FROM jobs WHERE invoice_id = :id ORDER BY created_at DESC LIMIT 1",
        id=invoice_id,
    )
    return job


def _status(engine: Engine, invoice_id: UUID) -> str:
    status: str = scalar(engine, "SELECT status FROM invoices WHERE id = :id", id=invoice_id)
    return status


def _proposal(engine: Engine, invoice_id: UUID) -> Any:
    (proposal,) = rows(engine, "SELECT * FROM proposals WHERE invoice_id = :id", id=invoice_id)
    return proposal


def _flag_codes(engine: Engine, invoice_id: UUID) -> list[str]:
    return [
        r.code
        for r in rows(
            engine, "SELECT code FROM flags WHERE invoice_id = :id ORDER BY code", id=invoice_id
        )
    ]


def _chain_ok(engine: Engine) -> bool:
    with engine.connect() as conn:
        return verify_chain(conn).ok


def _expire(owner_engine: Engine, job_id: UUID) -> None:
    with owner_engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"),
            {"id": job_id},
        )


class RecordingAgent:
    def __init__(self, *decisions: AgentDecision | None | BaseException):
        self.decisions = list(decisions)
        self.requests: list[AgentRequest] = []

    def __call__(self, request: AgentRequest) -> AgentDecision | None:
        self.requests.append(request)
        decision = self.decisions.pop(0) if self.decisions else None
        if isinstance(decision, BaseException):
            raise decision
        return decision


# --- happy paths -------------------------------------------------------------------------


def test_a_clean_invoice_runs_every_stage_to_pending_approval(
    seeded: ScratchDatabase, app_engine: Engine
) -> None:
    invoice_id = upload_seed(app_engine, CLEAN)
    agent = RecordingAgent()
    worker = _worker(app_engine, FakeProvider("gemini", _reply(CLEAN)), agent=agent)

    assert worker.run_once() is True

    assert _status(app_engine, invoice_id) == "pending_approval"
    job = _job(app_engine, invoice_id)
    assert (job.status, job.stage, job.attempts, job.lease_owner) == ("done", "propose", 1, None)
    assert set(job.stage_outputs) == {"extract", "rules", "agent", "propose"}
    proposal = _proposal(app_engine, invoice_id)
    assert (proposal.rule_floor, proposal.agent_action, proposal.final_action) == (
        "approve",
        None,
        "approve",
    )
    assert llm_calls(app_engine, invoice_id) == 1
    # Phase 3: the agent runs only when a rule raised a flag.
    assert agent.requests == []
    assert event_types(app_engine, invoice_id) == [
        "invoice.received",
        "extraction.completed",
        "rules.evaluated",
        "agent.skipped",
        "proposal.created",
    ]
    assert _chain_ok(app_engine)
    (run,) = rows(app_engine, "SELECT * FROM processing_runs WHERE invoice_id = :id", id=invoice_id)
    assert run.finished_at is not None and run.agent_model is None
    assert worker.run_once() is False


def test_a_flagged_invoice_reaches_the_agent_and_keeps_the_rule_floor(
    seeded: ScratchDatabase, app_engine: Engine
) -> None:
    invoice_id = upload_seed(app_engine, IBAN_SWAP)
    agent = RecordingAgent(None)
    worker = _worker(app_engine, FakeProvider("gemini", _reply(IBAN_SWAP)), agent=agent)

    assert worker.run_once() is True

    (request,) = agent.requests
    assert request.invoice_id == invoice_id
    assert request.rule_floor == "clarify" and "iban_not_current" in request.flag_codes
    proposal = _proposal(app_engine, invoice_id)
    assert (proposal.rule_floor, proposal.agent_action, proposal.final_action) == (
        "clarify",
        None,
        "clarify",
    )
    assert request.proposal_id == proposal.id
    (skipped,) = rows(
        app_engine, "SELECT payload FROM audit_events WHERE event_type = 'agent.skipped'"
    )
    assert skipped.payload["reason"] == "no_decision"
    assert _status(app_engine, invoice_id) == "pending_approval"


@pytest.mark.parametrize(
    ("seed_id", "agent_action", "final"),
    [
        (IBAN_SWAP, "approve", "clarify"),  # the agent can never lower the floor
        (IBAN_SWAP, "clarify", "clarify"),
        (IBAN_SWAP, "block", "block"),  # ... but it can raise it
    ],
)
def test_the_policy_gate_takes_the_stricter_action(
    seeded: ScratchDatabase,
    app_engine: Engine,
    seed_id: str,
    agent_action: Action,
    final: Action,
) -> None:
    invoice_id = upload_seed(app_engine, seed_id)
    decision = AgentDecision(action=agent_action, rationale="Agent says so.", model="agent-x")
    agent = RecordingAgent(decision)
    worker = _worker(app_engine, FakeProvider("gemini", _reply(seed_id)), agent=agent)

    assert worker.run_once() is True

    proposal = _proposal(app_engine, invoice_id)
    assert (proposal.agent_action, proposal.final_action) == (agent_action, final)
    assert proposal.rationale == "Agent says so."
    (created,) = rows(
        app_engine, "SELECT payload FROM audit_events WHERE event_type = 'proposal.created'"
    )
    assert created.payload["final_action"] == final
    assert created.payload["agent_action"] == agent_action
    assert scalar(
        app_engine, "SELECT agent_model FROM processing_runs WHERE invoice_id = :id", id=invoice_id
    ) == ("agent-x")
    assert "agent.completed" in event_types(app_engine, invoice_id)


def test_an_extraction_flag_raises_the_floor_even_when_every_rule_passes(
    seeded: ScratchDatabase, app_engine: Engine
) -> None:
    # The address is not printed on the PDF: grounding fails, no rule looks at the address.
    invoice_id = upload_seed(app_engine, CLEAN)
    reply = _reply(CLEAN, vendor_address="1 Invented Street, Nowhere")
    worker = _worker(app_engine, FakeProvider("gemini", reply))

    assert worker.run_once() is True

    assert _flag_codes(app_engine, invoice_id) == ["grounding_failure"]
    proposal = _proposal(app_engine, invoice_id)
    assert (proposal.rule_floor, proposal.final_action) == ("clarify", "clarify")
    assert "grounding_failure" in proposal.rationale


def test_a_schema_failure_still_runs_the_document_rules_and_clarifies(
    seeded: ScratchDatabase, app_engine: Engine
) -> None:
    invoice_id = upload_seed(app_engine, CLEAN)
    worker = _worker(app_engine, FakeProvider("gemini", "not json at all"))

    assert worker.run_once() is True

    assert {"schema_failure", "missing_field"} <= set(_flag_codes(app_engine, invoice_id))
    assert _proposal(app_engine, invoice_id).final_action == "clarify"
    assert scalar(app_engine, "SELECT count(*) FROM extractions") == 0
    assert _status(app_engine, invoice_id) == "pending_approval"


def test_a_resend_processed_after_its_original_is_blocked_as_a_duplicate(
    seeded: ScratchDatabase, app_engine: Engine
) -> None:
    # INV-SEED-063 resends INV-SEED-024 with a reformatted number ("RE 356").
    original = upload_seed(app_engine, "INV-SEED-024")
    resend = upload_seed(app_engine, "INV-SEED-063")
    provider = FakeProvider("gemini", _reply("INV-SEED-024"), _reply("INV-SEED-063"))
    worker = _worker(app_engine, provider)

    assert worker.run_once() is True
    assert worker.run_once() is True

    assert _proposal(app_engine, original).final_action == "approve"
    assert _proposal(app_engine, resend).final_action == "block"
    assert "duplicate_confirmed" in _flag_codes(app_engine, resend)


# --- crash and resume (never pays twice) -------------------------------------------------


def test_a_crash_between_stages_resumes_without_calling_the_llm_again(
    seeded: ScratchDatabase,
    app_engine: Engine,
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invoice_id = upload_seed(app_engine, CLEAN)
    # One scripted reply only: a second extraction request would fail the run.
    provider = FakeProvider("gemini", _reply(CLEAN))
    gateway = sql_gateway(app_engine, provider)

    def crash(*args: Any, **kwargs: Any) -> Any:
        raise Crash()

    monkeypatch.setattr(pipeline, "run_rules", crash)
    with pytest.raises(Crash):
        _worker(app_engine, gateway, owner="w1").run_once()
    monkeypatch.undo()

    job = _job(app_engine, invoice_id)
    assert (job.status, job.stage, job.lease_owner) == ("running", "rules", "w1")
    assert llm_calls(app_engine, invoice_id) == 1

    # The dead worker's lease runs out; a new worker sweeps it up and resumes at "rules".
    _expire(owner_engine, job.id)
    assert _worker(app_engine, gateway, owner="w2").run_once() is True

    assert _status(app_engine, invoice_id) == "pending_approval"
    assert llm_calls(app_engine, invoice_id) == 1
    assert provider.calls and len(provider.calls) == 1
    job = _job(app_engine, invoice_id)
    assert (job.status, job.attempts) == ("done", 2)
    assert scalar(app_engine, "SELECT count(*) FROM extractions") == 1
    assert scalar(app_engine, "SELECT count(*) FROM processing_runs") == 1
    events = event_types(app_engine, invoice_id)
    assert events.count("extraction.completed") == 1
    assert "job.lease_expired" in events
    assert _chain_ok(app_engine)


def test_a_crash_after_the_rules_does_not_write_them_twice(
    seeded: ScratchDatabase, app_engine: Engine, owner_engine: Engine
) -> None:
    invoice_id = upload_seed(app_engine, IBAN_SWAP)
    gateway = sql_gateway(app_engine, FakeProvider("gemini", _reply(IBAN_SWAP)))
    with pytest.raises(Crash):
        _worker(app_engine, gateway, agent=RecordingAgent(Crash())).run_once()
    flags_before = scalar(app_engine, "SELECT count(*) FROM flags")
    assert _job(app_engine, invoice_id).stage == "agent"

    _expire(owner_engine, _job(app_engine, invoice_id).id)
    assert _worker(app_engine, gateway, owner="w2", agent=RecordingAgent(None)).run_once()

    assert scalar(app_engine, "SELECT count(*) FROM flags") == flags_before
    assert scalar(app_engine, "SELECT count(*) FROM proposals") == 1
    assert llm_calls(app_engine, invoice_id) == 1
    assert _status(app_engine, invoice_id) == "pending_approval"


def test_a_failed_stage_is_retried_from_where_it_stopped(
    seeded: ScratchDatabase, app_engine: Engine
) -> None:
    invoice_id = upload_seed(app_engine, IBAN_SWAP)
    agent = RecordingAgent(RuntimeError("agent broke"), None)
    worker = _worker(app_engine, FakeProvider("gemini", _reply(IBAN_SWAP)), agent=agent)

    assert worker.run_once() is True
    job = _job(app_engine, invoice_id)
    assert (job.status, job.stage, job.attempts) == ("queued", "agent", 1)
    assert job.last_error == "RuntimeError: agent broke"
    assert _status(app_engine, invoice_id) == "processing"

    assert worker.run_once() is True
    assert _status(app_engine, invoice_id) == "pending_approval"
    assert llm_calls(app_engine, invoice_id) == 1
    events = event_types(app_engine, invoice_id)
    assert events.count("job.retry_scheduled") == 1
    (retry,) = rows(
        app_engine, "SELECT payload FROM audit_events WHERE event_type = 'job.retry_scheduled'"
    )
    # The audit payload names the error class only; exception text stays in jobs.last_error.
    assert retry.payload["error"] == "RuntimeError" and retry.payload["attempt"] == 1


def test_a_new_job_reuses_the_stored_extraction_of_the_same_prompt_version(
    seeded: ScratchDatabase, app_engine: Engine
) -> None:
    invoice_id = upload_seed(app_engine, CLEAN)
    provider = FakeProvider("gemini", _reply(CLEAN))
    worker = _worker(app_engine, provider)
    assert worker.run_once() is True
    with app_engine.begin() as conn:
        assert queue.enqueue(conn, invoice_id) is not None

    assert worker.run_once() is True

    assert llm_calls(app_engine, invoice_id) == 1
    assert scalar(app_engine, "SELECT count(*) FROM proposals") == 1
    (_, second) = rows(
        app_engine,
        "SELECT payload FROM audit_events WHERE event_type = 'extraction.completed' ORDER BY seq",
    )
    assert second.payload["reused"] is True
    assert _status(app_engine, invoice_id) == "pending_approval"


# --- quota -------------------------------------------------------------------------------


def test_quota_exhaustion_parks_the_invoice_until_the_reset(
    seeded: ScratchDatabase, app_engine: Engine
) -> None:
    invoice_id = upload_seed(app_engine, CLEAN)
    provider = FakeProvider(
        "gemini", QuotaExhausted("gemini", status=429, period="day", reset_in_s=3600)
    )
    worker = _worker(app_engine, provider)

    assert worker.run_once() is True

    assert _status(app_engine, invoice_id) == "awaiting_quota"
    job = _job(app_engine, invoice_id)
    reset = scalar(app_engine, "SELECT exhausted_until FROM budget_state WHERE provider = 'gemini'")
    assert (job.status, job.stage, job.lease_owner) == ("queued", "extract", None)
    assert job.run_after == reset
    # Waiting for quota is not a failed attempt.
    assert job.attempts == 0
    events = event_types(app_engine, invoice_id)
    assert events[-1] == "invoice.awaiting_quota" and "job.failed" not in events
    (parked,) = rows(
        app_engine, "SELECT payload FROM audit_events WHERE event_type = 'invoice.awaiting_quota'"
    )
    assert parked.payload["providers"] == ["gemini"]
    assert datetime.fromisoformat(parked.payload["retry_at"]) == reset
    # Not claimable before the reset.
    assert worker.run_once() is False

    # The quota is back: the same job resumes and completes.
    with app_engine.begin() as conn:
        conn.execute(text("UPDATE budget_state SET exhausted_until = now() - interval '1s'"))
        conn.execute(text("UPDATE jobs SET run_after = now() - interval '1s'"))
    provider.script.append(_reply(CLEAN))
    assert worker.run_once() is True
    assert _status(app_engine, invoice_id) == "pending_approval"
    assert llm_calls(app_engine, invoice_id) == 2  # the refused call and the good one
    assert _job(app_engine, invoice_id).attempts == 1


def test_a_soft_cap_parks_the_invoice_until_the_next_utc_day(
    seeded: ScratchDatabase, app_engine: Engine
) -> None:
    invoice_id = upload_seed(app_engine, CLEAN)
    gateway = sql_gateway(app_engine, FakeProvider("gemini"), llm_daily_soft_cap_gemini=0)
    before = datetime.now(UTC)
    assert _worker(app_engine, gateway).run_once() is True

    assert _status(app_engine, invoice_id) == "awaiting_quota"
    assert llm_calls(app_engine, invoice_id) == 0
    tomorrow = (before + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    assert _job(app_engine, invoice_id).run_after == tomorrow


# --- retries -----------------------------------------------------------------------------


def test_errors_are_retried_a_bounded_number_of_times_then_the_job_fails(
    seeded: ScratchDatabase, app_engine: Engine
) -> None:
    invoice_id = upload_seed(app_engine, CLEAN)
    # The gateway itself retries a transient error twice: three requests per attempt.
    provider = FakeProvider("gemini", *(TransientError("gemini", status=503) for _ in range(9)))
    worker = _worker(app_engine, provider, max_attempts=3)

    for attempt in (1, 2):
        assert worker.run_once() is True
        job = _job(app_engine, invoice_id)
        assert (job.status, job.attempts) == ("queued", attempt)
        assert _status(app_engine, invoice_id) == "processing"
    assert worker.run_once() is True

    job = _job(app_engine, invoice_id)
    assert (job.status, job.attempts, job.lease_owner) == ("failed", 3, None)
    assert "transient" in job.last_error
    assert _status(app_engine, invoice_id) == "failed"
    assert llm_calls(app_engine, invoice_id) == 9
    events = event_types(app_engine, invoice_id)
    assert events.count("job.retry_scheduled") == 2 and events[-1] == "job.failed"
    assert worker.run_once() is False
    assert _chain_ok(app_engine)


def test_a_retry_waits_for_its_backoff(seeded: ScratchDatabase, app_engine: Engine) -> None:
    invoice_id = upload_seed(app_engine, CLEAN)
    provider = FakeProvider("gemini", *(TransientError("gemini", status=503) for _ in range(3)))
    worker = _worker(app_engine, provider, retry_backoff_s=600.0)
    assert worker.run_once() is True
    job = _job(app_engine, invoice_id)
    assert job.status == "queued"
    assert job.run_after > datetime.now(UTC) + timedelta(minutes=9)
    assert worker.run_once() is False


# --- leases during a run -----------------------------------------------------------------


def test_a_worker_that_lost_its_lease_writes_nothing_more(
    seeded: ScratchDatabase, app_engine: Engine, owner_engine: Engine
) -> None:
    invoice_id = upload_seed(app_engine, IBAN_SWAP)
    gateway = sql_gateway(app_engine, FakeProvider("gemini", _reply(IBAN_SWAP)))

    def lose_the_lease(request: AgentRequest) -> AgentDecision | None:
        # While the agent "thinks", the lease runs out and the sweep hands the job back.
        _expire(owner_engine, _job(app_engine, invoice_id).id)
        with app_engine.begin() as conn:
            queue.sweep_expired(conn, max_attempts=3)
        return AgentDecision(action="approve", rationale="late")

    assert _worker(app_engine, gateway, agent=lose_the_lease).run_once() is True
    job = _job(app_engine, invoice_id)
    assert (job.status, job.stage, job.lease_owner) == ("queued", "agent", None)
    assert "agent" not in job.stage_outputs
    assert "proposal.created" not in event_types(app_engine, invoice_id)

    assert _worker(app_engine, gateway, owner="w2", agent=RecordingAgent(None)).run_once()
    assert event_types(app_engine, invoice_id).count("proposal.created") == 1
    assert _proposal(app_engine, invoice_id).agent_action is None


def test_the_heartbeat_keeps_a_slow_jobs_lease(seeded: ScratchDatabase, app_engine: Engine) -> None:
    invoice_id = upload_seed(app_engine, IBAN_SWAP)
    swept: list[queue.Sweep] = []

    def slow(request: AgentRequest) -> AgentDecision | None:
        time.sleep(1.5)  # longer than the one-second lease
        with app_engine.begin() as conn:
            swept.append(queue.sweep_expired(conn, max_attempts=3))
        return None

    worker = _worker(
        app_engine,
        FakeProvider("gemini", _reply(IBAN_SWAP)),
        agent=slow,
        lease_s=1.0,
        heartbeat_s=0.1,
    )
    assert worker.run_once() is True
    assert swept[0].requeued == ()
    assert _status(app_engine, invoice_id) == "pending_approval"


def test_a_stop_between_stages_hands_the_job_back(
    seeded: ScratchDatabase, app_engine: Engine
) -> None:
    invoice_id = upload_seed(app_engine, IBAN_SWAP)
    gateway = sql_gateway(app_engine, FakeProvider("gemini", _reply(IBAN_SWAP)))
    holder: list[Worker] = []

    def stop_now(request: AgentRequest) -> AgentDecision | None:
        holder[0].stop()
        return None

    worker = _worker(app_engine, gateway, agent=stop_now)
    holder.append(worker)
    assert worker.run_once() is True

    job = _job(app_engine, invoice_id)
    assert (job.status, job.stage, job.lease_owner, job.attempts) == ("queued", "propose", None, 0)
    assert _worker(app_engine, gateway, owner="w2").run_once() is True
    assert _status(app_engine, invoice_id) == "pending_approval"


# --- the loop ----------------------------------------------------------------------------


@pytest.fixture
def running_worker(seeded: ScratchDatabase, app_engine: Engine) -> Iterator[Worker]:
    worker = _worker(app_engine, FakeProvider("gemini", _reply(CLEAN)), poll_s=0.05)
    worker.start()
    try:
        yield worker
    finally:
        worker.stop()


def test_the_background_thread_processes_a_queued_invoice(
    running_worker: Worker, app_engine: Engine
) -> None:
    invoice_id = upload_seed(app_engine, CLEAN)
    deadline = time.monotonic() + 15
    while _status(app_engine, invoice_id) != "pending_approval":
        assert time.monotonic() < deadline, "the worker thread did not finish the job"
        time.sleep(0.05)
    assert running_worker.is_alive()
    running_worker.stop()
    assert not running_worker.is_alive()


def test_the_loop_survives_a_database_error(
    app_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _worker(app_engine, FakeProvider("gemini"), poll_s=0.01)
    calls: list[int] = []

    def broken() -> bool:
        calls.append(1)
        raise RuntimeError("db down")

    monkeypatch.setattr(worker, "run_once", broken)
    thread = threading.Thread(target=worker._loop, daemon=True)
    thread.start()
    time.sleep(0.2)
    worker.stop()
    thread.join(2)
    assert not thread.is_alive() and len(calls) > 1


# --- quota reset time and bad state ------------------------------------------------------


def _next_utc_midnight(now: datetime) -> datetime:
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def test_quota_retry_is_the_earliest_provider_back(app_engine: Engine) -> None:
    now = datetime.now(UTC)
    with app_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO budget_state (provider, exhausted_until) VALUES "
                "('gemini', now() + interval '1 second'), ('groq', now() + interval '3 days'), "
                "('anthropic', now() - interval '1 hour')"
            )
        )
        soon = conn.execute(
            text("SELECT exhausted_until FROM budget_state WHERE provider = 'gemini'")
        ).scalar_one()
        assert quota_retry_at(conn, ["gemini", "groq"]) == soon
        # An expired or missing budget_state row means the daily counts: next 00:00 UTC.
        assert quota_retry_at(conn, ["anthropic"]) == _next_utc_midnight(now)
        assert quota_retry_at(conn, ["groq", "anthropic"]) == _next_utc_midnight(now)
        assert quota_retry_at(conn, []) == _next_utc_midnight(now)


def test_a_job_at_an_unknown_stage_fails_instead_of_looping(
    seeded: ScratchDatabase, app_engine: Engine
) -> None:
    invoice_id = upload_seed(app_engine, CLEAN)
    with app_engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET stage = 'bogus'"))
    worker = _worker(app_engine, FakeProvider("gemini"), max_attempts=2)
    assert worker.run_once() is True
    assert worker.run_once() is True
    job = _job(app_engine, invoice_id)
    assert job.status == "failed" and "unknown stage" in job.last_error
    assert _status(app_engine, invoice_id) == "failed"
