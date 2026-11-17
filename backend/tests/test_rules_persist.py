"""persist_rule_results: flag rows and the proposal's rule floor, written as the app role."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Connection, Engine, text

from app.rules.checks import flag
from app.rules.persist import persist_rule_results
from tests.support import insert_extraction
from tests.test_matching import fields

INVOICE = str(uuid.UUID(int=7))


@pytest.fixture
def conn(app_engine: Engine) -> Iterator[Connection]:
    with app_engine.connect() as connection:
        yield connection


@pytest.fixture
def run_id(conn: Connection) -> str:
    return insert_extraction(conn, INVOICE, fields(), datetime(2026, 3, 1, tzinfo=UTC))


def test_flags_and_rule_floor_are_written(conn: Connection, run_id: str) -> None:
    flags = [
        flag("wrong_vat", "VAT is wrong.", check="rate", expected_rate="19.00"),
        flag("iban_not_current", "New IBAN.", iban="DE00", status="never_seen"),
    ]
    saved = persist_rule_results(conn, INVOICE, run_id, flags)
    assert saved.rule_floor == "clarify" and len(saved.flag_ids) == 2

    rows = conn.execute(
        text("SELECT id, code, severity, evidence, run_id FROM flags ORDER BY code")
    ).all()
    assert [(r.code, r.severity) for r in rows] == [
        ("iban_not_current", "critical"),
        ("wrong_vat", "warning"),
    ]
    assert {r.id for r in rows} == set(saved.flag_ids)
    assert rows[1].evidence == {
        "message": "VAT is wrong.",
        "action_floor": "clarify",
        "details": {"check": "rate", "expected_rate": "19.00"},
    }
    assert {str(r.run_id) for r in rows} == {run_id}

    proposal = conn.execute(text("SELECT * FROM proposals")).one()
    assert proposal.id == saved.proposal_id
    assert (proposal.rule_floor, proposal.agent_action, proposal.final_action) == (
        "clarify",
        None,
        "clarify",
    )
    assert proposal.rationale == "Rules raised 2 flags: wrong_vat, iban_not_current."


def test_no_flag_means_an_approve_floor(conn: Connection, run_id: str) -> None:
    saved = persist_rule_results(conn, INVOICE, run_id, [])
    assert saved.flag_ids == ()
    proposal = conn.execute(text("SELECT rule_floor, final_action, rationale FROM proposals")).one()
    assert tuple(proposal) == ("approve", "approve", "No rule raised a flag.")
    assert conn.execute(text("SELECT count(*) FROM flags")).scalar_one() == 0


def test_a_confirmed_duplicate_blocks(conn: Connection, run_id: str) -> None:
    saved = persist_rule_results(
        conn, INVOICE, run_id, [flag("duplicate_confirmed", "Same number.", invoice_id="X")]
    )
    assert saved.rule_floor == "block"
    assert conn.execute(text("SELECT final_action FROM proposals")).scalar_one() == "block"


def test_writes_join_the_callers_transaction(conn: Connection, run_id: str) -> None:
    conn.commit()  # keep the invoice and run, then roll back only the rule results
    persist_rule_results(conn, INVOICE, run_id, [flag("wrong_vat", "x")])
    conn.rollback()
    assert conn.execute(text("SELECT count(*) FROM flags")).scalar_one() == 0
    assert conn.execute(text("SELECT count(*) FROM proposals")).scalar_one() == 0
