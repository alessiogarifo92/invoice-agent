"""scripts/load_master_data.py: contract validation (no DB) and idempotent upsert (DB)."""

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from tests.support import FIXTURES, ScratchDatabase, load_script

loader = load_script("load_master_data")
FIXTURE = FIXTURES / "master.json"
TABLES = (
    "vendors",
    "vendor_bank_accounts",
    "purchase_orders",
    "po_lines",
    "contracts",
    "contract_prices",
    "contract_clauses",
    "vat_rates",
    "settings_kv",
)


def _raw() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    return data


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "master.json"
    path.write_text(content)
    return path


def _counts(url: str) -> dict[str, int]:
    with psycopg.connect(url) as conn:
        return {
            t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # type: ignore[index]
            for t in TABLES
        }


def test_fixture_parses() -> None:
    data = loader.read_master(FIXTURE)
    assert [v.id for v in data.vendors] == ["V001", "V002"]
    assert data.purchase_orders[1].lines[0].quantity == Decimal("2.5")
    assert data.vat_rates["IT"] == Decimal("22.00")
    assert data.vendor_bank_accounts[1].valid_to is None


def test_malformed_json_is_a_clear_error(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"vendors": [}')
    with pytest.raises(loader.MasterDataError, match=r"master\.json: invalid JSON at line 1"):
        loader.read_master(path)


def test_missing_file_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(loader.MasterDataError, match="cannot read"):
        loader.read_master(tmp_path / "nope.json")


def test_missing_top_level_key_names_the_key(tmp_path: Path) -> None:
    raw = _raw()
    del raw["po_tolerance_pct"]
    with pytest.raises(loader.MasterDataError, match=r"po_tolerance_pct: Field required"):
        loader.read_master(_write(tmp_path, json.dumps(raw)))


def test_missing_nested_key_names_its_path(tmp_path: Path) -> None:
    raw = _raw()
    del raw["purchase_orders"][1]["lines"][0]["unit_price"]
    with pytest.raises(
        loader.MasterDataError, match=r"purchase_orders\.1\.lines\.0\.unit_price: Field required"
    ):
        loader.read_master(_write(tmp_path, json.dumps(raw)))


def test_bad_decimal_and_date_are_rejected(tmp_path: Path) -> None:
    raw = _raw()
    raw["purchase_orders"][0]["total_net"] = "a lot"
    raw["contracts"][0]["valid_from"] = "2025-13-40"
    with pytest.raises(loader.MasterDataError) as excinfo:
        loader.read_master(_write(tmp_path, json.dumps(raw)))
    message = str(excinfo.value)
    assert "purchase_orders.0.total_net" in message
    assert "contracts.0.valid_from" in message


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    raw = _raw()
    raw["vendors"][0]["nickname"] = "alpha"
    with pytest.raises(loader.MasterDataError, match=r"vendors\.0\.nickname"):
        loader.read_master(_write(tmp_path, json.dumps(raw)))


def test_reference_to_an_unknown_vendor_is_rejected(tmp_path: Path) -> None:
    raw = _raw()
    raw["vendor_bank_accounts"][0]["vendor_id"] = "V999"
    with pytest.raises(loader.MasterDataError, match=r"vendor_bank_accounts\.0.*'V999'"):
        loader.read_master(_write(tmp_path, json.dumps(raw)))


def test_top_level_must_be_an_object(tmp_path: Path) -> None:
    with pytest.raises(loader.MasterDataError, match="Input should be"):
        loader.read_master(_write(tmp_path, "[]"))


def test_main_requires_the_owner_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OWNER_DATABASE_URL", raising=False)
    assert loader.main([str(FIXTURE)]) == 2


def test_main_reports_a_bad_file_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OWNER_DATABASE_URL", "postgresql://o@127.0.0.1:1/none")
    assert loader.main([str(_write(tmp_path, "{"))]) == 1
    assert "invalid JSON" in capsys.readouterr().err


def test_load_is_idempotent(test_db: ScratchDatabase) -> None:
    data = loader.read_master(FIXTURE)
    with psycopg.connect(test_db.url) as conn:
        loader.load(conn, data)
    first = _counts(test_db.url)
    assert first == {
        "vendors": 2,
        "vendor_bank_accounts": 3,
        "purchase_orders": 2,
        "po_lines": 3,
        "contracts": 1,
        "contract_prices": 1,
        "contract_clauses": 2,
        "vat_rates": 2,
        "settings_kv": 1,
    }
    with psycopg.connect(test_db.url) as conn:
        loader.load(conn, data)
    assert _counts(test_db.url) == first


def test_rerun_updates_changed_values(test_db: ScratchDatabase, tmp_path: Path) -> None:
    with psycopg.connect(test_db.url) as conn:
        loader.load(conn, loader.read_master(FIXTURE))
    raw = copy.deepcopy(_raw())
    raw["vendors"][0]["name"] = "Synthetic Alpha Renamed S.r.l."
    raw["vendor_bank_accounts"][1]["valid_to"] = "2026-12-31"
    raw["contracts"][0]["clauses"][0]["text"] = "Penalties of 2% per week apply."
    raw["po_tolerance_pct"] = "3.50"
    with psycopg.connect(test_db.url) as conn:
        loader.load(conn, loader.read_master(_write(tmp_path, json.dumps(raw))))
        assert conn.execute("SELECT name FROM vendors WHERE id = 'V001'").fetchone() == (
            "Synthetic Alpha Renamed S.r.l.",
        )
        valid_to = conn.execute(
            "SELECT valid_to::text FROM vendor_bank_accounts "
            "WHERE iban = 'IT02A0301503200000003517230'"
        ).fetchone()
        assert valid_to == ("2026-12-31",)
        assert conn.execute(
            "SELECT clause_no FROM contract_clauses WHERE tsv @@ plainto_tsquery('english', '2')"
        ).fetchone() == ("4.2",)
        assert conn.execute(
            "SELECT value FROM settings_kv WHERE key = 'po_tolerance_pct'"
        ).fetchone() == ("3.50",)


def test_loaded_decimals_keep_their_scale(test_db: ScratchDatabase) -> None:
    with psycopg.connect(test_db.url) as conn:
        loader.load(conn, loader.read_master(FIXTURE))
        row = conn.execute(
            "SELECT quantity, unit_price FROM po_lines WHERE po_id = 'PO-2026-0002' AND line_no = 1"
        ).fetchone()
        rate = conn.execute("SELECT rate_pct FROM vat_rates WHERE country = 'IT'").fetchone()
    assert row == (Decimal("2.5"), Decimal("100.20"))
    assert rate == (Decimal("22.00"),)


def test_main_loads_the_file(test_db: ScratchDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OWNER_DATABASE_URL", test_db.sqlalchemy_url)
    assert loader.main([str(FIXTURE)]) == 0
    assert _counts(test_db.url)["vendors"] == 2
