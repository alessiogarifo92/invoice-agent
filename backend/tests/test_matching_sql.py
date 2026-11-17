"""SQL-only matching: settings edge cases, contract-clause full-text search, pg_trgm parity."""

import json
from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, Engine, text

from app.matching import sql
from app.matching.memory import MemoryMatcher
from app.rules.numbers import trigram_similarity
from tests.support import FIXTURES, ScratchDatabase
from tests.test_matching import load_fixture


@pytest.fixture
def conn(test_db: ScratchDatabase, app_engine: Engine) -> Iterator[Connection]:
    load_fixture(test_db)
    with app_engine.connect() as connection:
        yield connection


def _set_tolerance(owner_engine: Engine, value: str | None) -> None:
    with owner_engine.begin() as owner:
        owner.execute(text("DELETE FROM settings_kv WHERE key = 'po_tolerance_pct'"))
        if value is not None:
            owner.execute(
                text("INSERT INTO settings_kv (key, value) VALUES ('po_tolerance_pct', :v)"),
                {"v": value},
            )


def test_missing_or_unreadable_tolerance_is_none(conn: Connection, owner_engine: Engine) -> None:
    _set_tolerance(owner_engine, "two percent")
    assert sql.po_tolerance_pct(conn) is None
    conn.rollback()
    _set_tolerance(owner_engine, None)
    assert sql.po_tolerance_pct(conn) is None


def test_memory_matcher_unreadable_tolerance_is_none() -> None:
    master = json.loads((FIXTURES / "master.json").read_text())
    assert MemoryMatcher.from_master({**master, "po_tolerance_pct": "x"}).po_tolerance_pct() is None


def test_clause_search_ranks_the_vendors_matching_clauses(conn: Connection) -> None:
    hits = sql.search_contract_clauses(conn, "V001", "change of bank accounts")
    assert [(h.contract_id, h.clause_no, h.title) for h in hits] == [
        ("C001", "7.1", "Bank details")
    ]
    assert hits[0].rank > 0
    assert "call-back" in hits[0].text


def test_clause_search_stems_and_orders_by_rank(conn: Connection) -> None:
    hits = sql.search_contract_clauses(conn, "V001", "late")
    assert [h.clause_no for h in hits] == ["4.2"]
    # Stop words only: an empty query matches nothing.
    assert sql.search_contract_clauses(conn, "V001", "the of and") == ()


def test_clause_search_is_bound_to_the_vendor(conn: Connection) -> None:
    assert sql.search_contract_clauses(conn, "V002", "bank account") == ()


def test_clause_search_returns_at_most_five(conn: Connection, owner_engine: Engine) -> None:
    with owner_engine.begin() as owner:
        for n in range(8):
            owner.execute(
                text(
                    'INSERT INTO contract_clauses (contract_id, clause_no, title, "text") '
                    "VALUES ('C001', :no, 'Invoices', :body)"
                ),
                {"no": f"9.{n}", "body": "invoice " * (n + 1)},
            )
    hits = sql.search_contract_clauses(conn, "V001", "invoice")
    assert len(hits) == 5
    assert [h.rank for h in hits] == sorted((h.rank for h in hits), reverse=True)


PAIRS = [
    ("RE356", "RE356"),
    ("RE356", "RE339"),
    ("F2026307", "F2026308"),
    ("INV141", "INV0141"),
    ("Beta Test Supplies", "Beta Test Supplies GmbH"),
    ("Maillot Trading Synthetica SARL", "Reynaud Electrical Synthetica SARL"),
    ("Posada Catering Synthetica S.L.", "posada catering"),
    ("a_b-c", "abc"),
    ("", "abc"),
    ("Ünïcödé Straße", "Unicode Strasse"),
]


@pytest.mark.parametrize(("a", "b"), PAIRS)
def test_trigram_similarity_equals_pg_trgm(conn: Connection, a: str, b: str) -> None:
    pg = conn.execute(
        text("SELECT CAST(similarity(:a, :b) AS double precision)"), {"a": a, "b": b}
    ).scalar_one()
    assert trigram_similarity(a, b) == pg
