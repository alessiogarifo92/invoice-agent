"""In-memory vs SQL matcher on the seed master data: the Layer-1 eval measures what the app does.

Every seed invoice is processed in issue order, as the eval does: both matchers build the
whole RuleContext, which must be equal, then both record the invoice's extraction.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from sqlalchemy import Engine

from app.matching.context import build_context
from app.matching.memory import MemoryMatcher
from app.matching.sql import SqlMatcher
from app.rules.facts import parse_facts
from tests.support import SEED_DIR, SEED_INVOICES, ScratchDatabase, insert_extraction, load_script

loader = load_script("load_master_data")


def _seed_in_issue_order() -> list[dict[str, Any]]:
    records = [json.loads(p.read_text()) for p in SEED_INVOICES.glob("*.json")]
    return sorted(records, key=lambda r: (r["fields"]["issue_date"], r["invoice_id"]))


def _created_at(n: int) -> datetime:
    return datetime(2026, 11, 1, tzinfo=UTC) + timedelta(seconds=n)


def _uuid(invoice_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, invoice_id))


def test_memory_and_sql_matchers_agree_on_the_seed(
    test_db: ScratchDatabase, app_engine: Engine
) -> None:
    with psycopg.connect(test_db.url) as owner:
        loader.load(owner, loader.read_master(SEED_DIR / "master.json"))
    master = json.loads((SEED_DIR / "master.json").read_text())
    memory = MemoryMatcher.from_master(master)
    records = _seed_in_issue_order()
    assert len(records) == 63

    with app_engine.connect() as conn:
        sql = SqlMatcher(conn)
        # Vendor lookups by each key alone, every account included.
        for vendor in master["vendors"]:
            for args in ((vendor["vat_id"], None, None), (None, None, vendor["name"])):
                assert memory.find_vendor(*args) == sql.find_vendor(*args)
        for account in master["vendor_bank_accounts"]:
            assert memory.find_vendor(None, account["iban"], None) == sql.find_vendor(
                None, account["iban"], None
            )

        duplicates_seen = 0
        for n, record in enumerate(records):
            invoice_id = _uuid(record["invoice_id"])
            facts = parse_facts(record["fields"])
            expected = build_context(memory, facts, invoice_id)
            assert build_context(sql, facts, invoice_id) == expected, record["invoice_id"]
            name_only = (None, None, facts.vendor_name)
            assert memory.find_vendor(*name_only) == sql.find_vendor(*name_only)
            duplicates_seen += len(expected.duplicates)

            memory.add_extraction(invoice_id, record["fields"])
            insert_extraction(conn, invoice_id, record["fields"], _created_at(n))

    # The resend found its original, so the comparison covered a real duplicate.
    assert duplicates_seen >= 1
