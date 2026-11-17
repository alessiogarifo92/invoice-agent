"""GET /proposals/{id} and POST /proposals/{id}/decision (T9, human in the loop), against a
scratch database as the app role. Proposals come from the real pipeline (worker + fake
provider + scripted agent), so the stored stage outputs have their real shape."""

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.agent import AgentDecision, AgentRequest
from app.agent.tools import Evidence
from app.api.auth import reviewer_id, token_hash
from app.audit import verify_chain
from app.jobs.worker import Worker
from app.review import store as review_store
from app.review.machine import AlreadyDecided
from tests.api_support import AUTH, TOKEN, api_client
from tests.extraction_support import seed_fields
from tests.jobs_support import load_seed_master, rows, scalar, sql_gateway, upload_seed
from tests.llm_support import FakeProvider
from tests.support import ScratchDatabase

CLEAN = "INV-SEED-001"
IBAN_SWAP = "INV-SEED-037"
REVIEWER = reviewer_id(token_hash(TOKEN))
VENDOR_OUTPUT = {"found": True, "vendor_id": "V-TEST", "current_iban": "NL00TEST0000000000"}


@dataclass(frozen=True)
class Case:
    invoice_id: UUID
    proposal_id: UUID


def _process(engine: Engine, seed_id: str, decision: AgentDecision | None) -> Case:
    invoice_id = upload_seed(engine, seed_id)

    def agent(request: AgentRequest) -> AgentDecision | None:
        return decision

    gateway = sql_gateway(engine, FakeProvider("gemini", json.dumps(seed_fields(seed_id))))
    worker = Worker(engine, gateway, owner="w1", agent=agent, retry_backoff_s=0.0)
    assert worker.run_once() is True
    status = scalar(engine, "SELECT status FROM invoices WHERE id = :id", id=invoice_id)
    assert status == "pending_approval"
    proposal_id = scalar(engine, "SELECT id FROM proposals WHERE invoice_id = :id", id=invoice_id)
    return Case(invoice_id, proposal_id)


@pytest.fixture
def seeded(test_db: ScratchDatabase) -> ScratchDatabase:
    load_seed_master(test_db)
    return test_db


@pytest.fixture
def clean(seeded: ScratchDatabase, app_engine: Engine) -> Case:
    """No flag: the agent is skipped and the proposal is approve."""
    return _process(app_engine, CLEAN, None)


@pytest.fixture
def iban_swap(seeded: ScratchDatabase, app_engine: Engine) -> Case:
    """Changed IBAN (floor clarify); the agent cites one real tool result and says block."""
    decision = AgentDecision(
        action="block",
        rationale="The IBAN is not the vendor's.",
        evidence_refs=("ev-t-1",),
        evidence=(
            Evidence("ev-t-1", "get_vendor", ("V-TEST",), VENDOR_OUTPUT),
            Evidence("ev-t-2", "check_vat", (), {"ok": True}),
        ),
    )
    return _process(app_engine, IBAN_SWAP, decision)


@pytest.fixture
def client(app_engine: Engine) -> Iterator[TestClient]:
    with api_client(app_engine, headers=AUTH) as c:
        yield c


@pytest.fixture
def anon(app_engine: Engine) -> Iterator[TestClient]:
    with api_client(app_engine) as c:
        yield c


def _etag(client: TestClient, proposal_id: UUID) -> str:
    response = client.get(f"/proposals/{proposal_id}")
    assert response.status_code == 200, response.text
    etag: str = response.headers["etag"]
    return etag


def _decide(
    client: TestClient,
    proposal_id: UUID,
    action: str,
    etag: str | None,
    note: str | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    sent = dict(headers or {})
    if etag is not None:
        sent["If-Match"] = etag
    body: dict[str, Any] = {"action": action}
    if note is not None:
        body["note"] = note
    return client.post(f"/proposals/{proposal_id}/decision", json=body, headers=sent)


def _assert_error(response: Any, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code
    assert isinstance(body["message"], str) and body["message"]


def _status(engine: Engine, case: Case) -> str:
    status: str = scalar(engine, "SELECT status FROM invoices WHERE id = :id", id=case.invoice_id)
    return status


def _decisions(engine: Engine) -> list[Any]:
    return rows(engine, "SELECT * FROM approvals ORDER BY decided_at")


def _decided_events(engine: Engine) -> list[Any]:
    return rows(
        engine,
        "SELECT actor, entity_id, payload FROM audit_events "
        "WHERE event_type = 'review.decided' ORDER BY seq",
    )


def _assert_untouched(engine: Engine, case: Case) -> None:
    assert _status(engine, case) == "pending_approval"
    assert _decisions(engine) == []
    assert _decided_events(engine) == []


# --- GET /proposals/{id} -------------------------------------------------------------------


def test_get_returns_the_proposal_its_flags_and_resolved_evidence_with_an_etag(
    anon: TestClient, app_engine: Engine, iban_swap: Case
) -> None:
    response = anon.get(f"/proposals/{iban_swap.proposal_id}")
    assert response.status_code == 200, response.text
    version = scalar(
        app_engine, "SELECT version FROM invoices WHERE id = :id", id=iban_swap.invoice_id
    )
    assert response.headers["etag"] == f'"{iban_swap.proposal_id}.{version}"'

    body = response.json()
    assert body["id"] == str(iban_swap.proposal_id)
    assert body["invoice_id"] == str(iban_swap.invoice_id)
    assert body["invoice_status"] == "pending_approval"
    assert (body["rule_floor"], body["agent_action"], body["final_action"]) == (
        "clarify",
        "block",
        "block",
    )
    assert body["is_current"] is True and body["decision"] is None
    assert body["rationale"] == "The IBAN is not the vendor's."

    codes = {f["code"]: f for f in body["flags"]}
    flag = codes["iban_not_current"]
    assert (flag["severity"], flag["action_floor"]) == ("critical", "clarify")
    assert isinstance(flag["message"], str) and flag["message"]
    assert isinstance(flag["details"], dict)

    # Only the cited ref, resolved to the tool output stored by the agent stage.
    assert body["evidence"] == [
        {"ref": "ev-t-1", "tool": "get_vendor", "records": ["V-TEST"], "output": VENDOR_OUTPUT}
    ]


def test_get_a_proposal_the_agent_skipped_has_no_evidence(anon: TestClient, clean: Case) -> None:
    body = anon.get(f"/proposals/{clean.proposal_id}").json()
    assert (body["rule_floor"], body["agent_action"], body["final_action"]) == (
        "approve",
        None,
        "approve",
    )
    assert body["flags"] == [] and body["evidence"] == []


def test_get_unknown_proposal_is_404(anon: TestClient, test_db: ScratchDatabase) -> None:
    _assert_error(anon.get("/proposals/00000000-0000-0000-0000-000000000000"), 404, "not_found")


def test_get_with_an_invalid_uuid_is_422(anon: TestClient, test_db: ScratchDatabase) -> None:
    assert anon.get("/proposals/not-a-uuid").status_code == 422


def test_invoice_detail_links_its_current_proposal(anon: TestClient, clean: Case) -> None:
    body = anon.get(f"/invoices/{clean.invoice_id}").json()
    assert body["proposal_id"] == str(clean.proposal_id)


def test_invoice_list_shows_the_case_card_fields(anon: TestClient, iban_swap: Case) -> None:
    body = anon.get("/invoices").json()
    assert body["total"] == 1
    (item,) = body["items"]
    fields = seed_fields(IBAN_SWAP)
    assert item["id"] == str(iban_swap.invoice_id)
    assert item["status"] == "pending_approval"
    assert (item["proposal_id"], item["final_action"]) == (str(iban_swap.proposal_id), "block")
    assert (item["vendor_name"], item["invoice_number"]) == (
        fields["vendor_name"],
        fields["invoice_number"],
    )
    assert (item["total"], item["currency"]) == (fields["total"], fields["currency"])


# --- the demo token --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": TOKEN},
        {"X-Demo-Token": TOKEN},
    ],
)
def test_decision_without_a_valid_token_is_401(
    anon: TestClient, app_engine: Engine, clean: Case, headers: dict[str, str]
) -> None:
    etag = _etag(anon, clean.proposal_id)
    response = _decide(anon, clean.proposal_id, "approve", etag, headers=headers)
    _assert_error(response, 401, "unauthorized")
    assert response.headers["www-authenticate"] == "Bearer"
    _assert_untouched(app_engine, clean)


def test_the_token_is_checked_before_the_body(anon: TestClient, clean: Case) -> None:
    response = anon.post(f"/proposals/{clean.proposal_id}/decision", json={"action": "pay"})
    _assert_error(response, 401, "unauthorized")


def test_no_configured_hash_means_every_decision_is_401(app_engine: Engine, clean: Case) -> None:
    with api_client(app_engine, headers=AUTH, demo_token_hashes="") as c:
        etag = _etag(c, clean.proposal_id)
        _assert_error(_decide(c, clean.proposal_id, "approve", etag), 401, "unauthorized")


# --- If-Match ---------------------------------------------------------------------------------


def test_decision_without_if_match_is_428(
    client: TestClient, app_engine: Engine, clean: Case
) -> None:
    _assert_error(_decide(client, clean.proposal_id, "approve", None), 428, "precondition_required")
    _assert_error(_decide(client, clean.proposal_id, "approve", "*"), 428, "precondition_required")
    _assert_untouched(app_engine, clean)


def test_decision_with_a_stale_if_match_is_412(
    client: TestClient, app_engine: Engine, clean: Case
) -> None:
    etag = _etag(client, clean.proposal_id)
    for stale in (f'"{clean.proposal_id}.0"', f"W/{etag}", etag.strip('"'), '"other"'):
        _assert_error(_decide(client, clean.proposal_id, "approve", stale), 412, "stale_proposal")
    _assert_untouched(app_engine, clean)


def test_if_match_may_list_several_etags(client: TestClient, clean: Case) -> None:
    etag = _etag(client, clean.proposal_id)
    response = _decide(client, clean.proposal_id, "approve", f'"old", {etag}')
    assert response.status_code == 200, response.text


def test_decision_on_an_unknown_proposal_is_404(
    client: TestClient, test_db: ScratchDatabase
) -> None:
    missing = UUID("00000000-0000-0000-0000-000000000000")
    _assert_error(_decide(client, missing, "approve", '"x"'), 404, "not_found")


# --- decisions --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "status"),
    [
        ("approve", "approved_for_payment"),
        ("clarify", "clarification_requested"),
        ("block", "blocked"),
    ],
)
def test_a_decision_sets_the_status_records_the_reviewer_and_is_audited(
    client: TestClient,
    app_engine: Engine,
    owner_engine: Engine,
    clean: Case,
    action: str,
    status: str,
) -> None:
    etag = _etag(client, clean.proposal_id)
    response = _decide(client, clean.proposal_id, action, etag)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["invoice_status"] == status == _status(app_engine, clean)
    assert (body["action"], body["proposed_action"]) == (action, "approve")
    assert body["override"] is (action != "approve")
    assert body["lowered"] is False
    assert body["decided_by"] == REVIEWER

    # The new ETag is the one a fresh GET returns.
    assert response.headers["etag"] != etag
    assert response.headers["etag"] == _etag(client, clean.proposal_id)

    (decision,) = _decisions(app_engine)
    assert (decision.proposal_id, decision.decision, decision.decided_by) == (
        clean.proposal_id,
        action,
        REVIEWER,
    )
    (event,) = _decided_events(app_engine)
    assert event.actor == REVIEWER and event.entity_id == str(clean.invoice_id)
    assert event.payload == {
        "proposal_id": str(clean.proposal_id),
        "approval_id": str(decision.id),
        "action": action,
        "from_status": "pending_approval",
        "to_status": status,
        "rule_floor": "approve",
        "agent_action": None,
        "final_action": "approve",
        "override": action != "approve",
        "lowered": False,
        "note": None,
    }
    verified = client.get("/audit/verify").json()
    assert verified["ok"] is True
    head = scalar(app_engine, "SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1")
    assert verified["head_hash"] == head

    # The raw token is stored nowhere: only its fingerprint.
    with owner_engine.connect() as conn:
        dump = conn.execute(
            text(
                "SELECT string_agg(t::text, '') FROM ("
                "SELECT row_to_json(a) AS t FROM audit_events a UNION ALL "
                "SELECT row_to_json(p) FROM approvals p) s"
            )
        ).scalar_one()
    assert TOKEN not in dump and token_hash(TOKEN) not in dump

    after = client.get(f"/proposals/{clean.proposal_id}").json()
    assert after["invoice_status"] == status
    assert after["decision"]["action"] == action
    assert after["decision"]["decided_by"] == REVIEWER


def test_approve_never_pays_anything(client: TestClient, app_engine: Engine, clean: Case) -> None:
    etag = _etag(client, clean.proposal_id)
    assert _decide(client, clean.proposal_id, "approve", etag).status_code == 200
    # approved_for_payment is the end of the line: no job, no call, no other event follows.
    events = [
        r.event_type
        for r in rows(
            app_engine,
            "SELECT event_type FROM audit_events WHERE entity_id = :id ORDER BY seq",
            id=str(clean.invoice_id),
        )
    ]
    assert events[-1] == "review.decided"
    assert (
        scalar(
            app_engine,
            "SELECT count(*) FROM jobs WHERE invoice_id = :id AND status IN ('queued', 'running')",
            id=clean.invoice_id,
        )
        == 0
    )


def test_overriding_down_needs_a_note_and_audits_both_actions(
    client: TestClient, app_engine: Engine, iban_swap: Case
) -> None:
    etag = _etag(client, iban_swap.proposal_id)
    for note in (None, "", " \n\u200b "):
        response = _decide(client, iban_swap.proposal_id, "approve", etag, note=note)
        _assert_error(response, 422, "note_required")
    _assert_untouched(app_engine, iban_swap)

    note = "Called the vendor on the number\u202e in our record:\r\nthe new IBAN is genuine."
    response = _decide(client, iban_swap.proposal_id, "approve", etag, note=note)
    assert response.status_code == 200, response.text
    stored = "Called the vendor on the number in our record:\nthe new IBAN is genuine."
    body = response.json()
    assert (body["action"], body["proposed_action"], body["override"], body["lowered"]) == (
        "approve",
        "block",
        True,
        True,
    )
    assert body["note"] == stored
    (decision,) = _decisions(app_engine)
    assert decision.note == stored
    (event,) = _decided_events(app_engine)
    payload = event.payload
    assert (payload["action"], payload["final_action"], payload["rule_floor"]) == (
        "approve",
        "block",
        "clarify",
    )
    assert (payload["agent_action"], payload["override"], payload["lowered"]) == (
        "block",
        True,
        True,
    )
    assert payload["note"] == stored
    assert _status(app_engine, iban_swap) == "approved_for_payment"


def test_a_note_longer_than_the_limit_is_422(client: TestClient, clean: Case) -> None:
    etag = _etag(client, clean.proposal_id)
    response = _decide(client, clean.proposal_id, "approve", etag, note="x" * 2001)
    assert response.status_code == 422


def test_an_unknown_action_is_422(client: TestClient, app_engine: Engine, clean: Case) -> None:
    etag = _etag(client, clean.proposal_id)
    for action in ("pay", "APPROVE", ""):
        assert _decide(client, clean.proposal_id, action, etag).status_code == 422
    _assert_untouched(app_engine, clean)


def test_deciding_twice_is_refused(client: TestClient, app_engine: Engine, clean: Case) -> None:
    etag = _etag(client, clean.proposal_id)
    first = _decide(client, clean.proposal_id, "approve", etag)
    assert first.status_code == 200
    # The old ETag is stale now; the new one is current, but the proposal is decided.
    _assert_error(_decide(client, clean.proposal_id, "block", etag), 412, "stale_proposal")
    second = _decide(client, clean.proposal_id, "block", first.headers["etag"])
    _assert_error(second, 409, "already_decided")
    assert len(_decisions(app_engine)) == 1
    assert _status(app_engine, clean) == "approved_for_payment"


def test_deciding_on_an_invoice_that_is_not_pending_is_409(
    client: TestClient, owner_engine: Engine, app_engine: Engine, clean: Case
) -> None:
    with owner_engine.begin() as conn:
        conn.execute(
            text("UPDATE invoices SET status = 'processing', version = version + 1 WHERE id = :id"),
            {"id": clean.invoice_id},
        )
    etag = _etag(client, clean.proposal_id)
    _assert_error(_decide(client, clean.proposal_id, "approve", etag), 409, "invalid_transition")
    assert _decisions(app_engine) == []


def test_a_superseded_proposal_cannot_be_decided(
    client: TestClient, owner_engine: Engine, app_engine: Engine, clean: Case
) -> None:
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO proposals (invoice_id, run_id, rule_floor, final_action, rationale, "
                "created_at) SELECT invoice_id, run_id, 'clarify', 'clarify', 'newer', "
                "created_at + interval '1 second' FROM proposals WHERE id = :id"
            ),
            {"id": clean.proposal_id},
        )
    assert client.get(f"/proposals/{clean.proposal_id}").json()["is_current"] is False
    etag = _etag(client, clean.proposal_id)
    _assert_error(_decide(client, clean.proposal_id, "approve", etag), 409, "proposal_superseded")
    _assert_untouched(app_engine, clean)


def test_two_decisions_with_the_same_etag_one_wins_one_is_412(
    client: TestClient, app_engine: Engine, clean: Case
) -> None:
    etag = _etag(client, clean.proposal_id)
    first = _decide(client, clean.proposal_id, "approve", etag)
    second = _decide(client, clean.proposal_id, "block", etag)
    assert (first.status_code, second.status_code) == (200, 412)
    assert len(_decisions(app_engine)) == 1


def test_concurrent_decisions_with_the_same_etag_commit_exactly_one(
    app_engine: Engine, clean: Case
) -> None:
    with app_engine.connect() as conn:
        view = review_store.load_proposal(conn, clean.proposal_id)
    assert view is not None
    etag = review_store.proposal_etag(view.id, view.invoice_version)
    n = 4
    barrier = threading.Barrier(n)
    results: list[review_store.DecisionResult] = []
    errors: list[BaseException] = []

    def decide(action: str) -> None:
        try:
            with app_engine.connect() as conn:
                barrier.wait()
                results.append(
                    review_store.record_decision(
                        conn, clean.proposal_id, action, None, REVIEWER, frozenset({etag})
                    )
                )
        except BaseException as exc:  # surfaced in the main thread below
            errors.append(exc)

    threads = [
        threading.Thread(target=decide, args=(a,))
        for a in ("approve", "block", "clarify", "approve")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 1
    assert len(errors) == n - 1
    assert all(isinstance(e, review_store.StaleProposal) for e in errors), errors
    assert len(_decisions(app_engine)) == 1
    assert len(_decided_events(app_engine)) == 1
    with app_engine.connect() as conn:
        assert verify_chain(conn).ok is True


def test_record_decision_refuses_a_decided_proposal_even_with_a_current_etag(
    app_engine: Engine, owner_engine: Engine, clean: Case
) -> None:
    """Belt and braces: an approval row for the proposal blocks a second one, whatever the
    invoice status says (e.g. a status put back to pending_approval by hand)."""
    with app_engine.connect() as conn:
        view = review_store.load_proposal(conn, clean.proposal_id)
        assert view is not None
        review_store.record_decision(
            conn,
            clean.proposal_id,
            "approve",
            None,
            REVIEWER,
            frozenset({review_store.proposal_etag(view.id, view.invoice_version)}),
        )
    with owner_engine.begin() as conn:
        conn.execute(
            text("UPDATE invoices SET status = 'pending_approval' WHERE id = :id"),
            {"id": clean.invoice_id},
        )
    with app_engine.connect() as conn:
        view = review_store.load_proposal(conn, clean.proposal_id)
        assert view is not None
        with pytest.raises(AlreadyDecided):
            review_store.record_decision(
                conn,
                clean.proposal_id,
                "block",
                None,
                REVIEWER,
                frozenset({review_store.proposal_etag(view.id, view.invoice_version)}),
            )
    assert len(_decisions(app_engine)) == 1
