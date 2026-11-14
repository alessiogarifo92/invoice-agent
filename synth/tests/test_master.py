import random
import re
from collections import Counter

from synth.iban import FAKE_BANK_CODES, bank_code, is_valid_mod97
from synth.master import COUNTRIES, FICTIONAL_MARK, build_master
from synth.model import MasterData, master_to_json

SEED = 20261110


def _master() -> MasterData:
    return build_master(random.Random(SEED), SEED)


def test_vendors_are_about_twelve_across_five_eu_countries() -> None:
    master = _master()
    assert 10 <= len(master.vendors) <= 14
    countries = Counter(v.country for v in master.vendors)
    assert set(countries) == {"IT", "DE", "FR", "ES", "NL"}
    assert [v.id for v in master.vendors] == [
        f"V{i:03d}" for i in range(1, len(master.vendors) + 1)
    ]


def test_vendor_names_are_marked_fictional_and_unique() -> None:
    master = _master()
    names = [v.name for v in master.vendors]
    assert len(set(names)) == len(names)
    assert all(FICTIONAL_MARK in n for n in names)


def test_vat_ids_keep_country_prefix_with_obviously_fake_digits() -> None:
    master = _master()
    for v in master.vendors:
        assert v.vat_id.startswith(v.country)
        assert re.fullmatch(rf"{v.country}99\d+(B01)?", v.vat_id), v.vat_id
    assert len({v.vat_id for v in master.vendors}) == len(master.vendors)


def test_every_vendor_has_exactly_one_current_iban_and_some_have_history() -> None:
    master = _master()
    for v in master.vendors:
        current = [a for a in master.bank_accounts if a.vendor_id == v.id and a.valid_to is None]
        assert len(current) == 1
    with_history = [v for v in master.vendors if master.past_ibans(v.id)]
    assert len(with_history) >= 3
    for a in master.bank_accounts:
        assert is_valid_mod97(a.iban)
        assert bank_code(a.iban) in FAKE_BANK_CODES
        if a.valid_to is not None:
            # the past account closes before the current one opens
            nxt = master.current_iban(a.vendor_id)
            cur = next(b for b in master.bank_accounts if b.iban == nxt)
            assert a.valid_to < cur.valid_from


def test_po_lines_use_contract_prices_of_the_same_vendor() -> None:
    master = _master()
    assert master.purchase_orders
    for po in master.purchase_orders:
        contract = master.contract_for(po.vendor_id)
        assert po.lines
        assert [ln.line_no for ln in po.lines] == list(range(1, len(po.lines) + 1))
        for ln in po.lines:
            assert ln.quantity > 0
            assert ln.unit_price == contract.price_of(ln.sku)
        assert contract.valid_from <= po.issue_date <= contract.valid_to


def test_contracts_have_prices_and_the_expected_clause_kinds() -> None:
    master = _master()
    assert len(master.contracts) == len(master.vendors)
    for c in master.contracts:
        assert len(c.prices) >= 3
        titles = " ".join(cl.title.lower() for cl in c.clauses)
        for kind in ("payment terms", "price adjustment", "late delivery", "bank details"):
            assert kind in titles
        assert f"{c.payment_days} days" in " ".join(cl.text for cl in c.clauses)


def test_skus_belong_to_a_single_vendor() -> None:
    master = _master()
    owners: dict[str, str] = {}
    for c in master.contracts:
        for p in c.prices:
            assert owners.setdefault(p.sku, c.vendor_id) == c.vendor_id


def test_master_json_follows_the_output_contract() -> None:
    data = master_to_json(_master())
    assert list(data) == [
        "vendors",
        "vendor_bank_accounts",
        "purchase_orders",
        "contracts",
        "vat_rates",
        "po_tolerance_pct",
    ]
    assert data["vat_rates"] == {
        "IT": "22.00",
        "DE": "19.00",
        "FR": "20.00",
        "ES": "21.00",
        "NL": "21.00",
    }
    assert data["po_tolerance_pct"] == "2.00"
    assert set(data["vendors"][0]) == {"id", "name", "vat_id", "country", "address"}
    acct = data["vendor_bank_accounts"][0]
    assert set(acct) == {"vendor_id", "iban", "valid_from", "valid_to"}
    po = data["purchase_orders"][0]
    assert set(po) == {"id", "vendor_id", "currency", "issue_date", "total_net", "lines"}
    assert re.fullmatch(r"PO-2026-\d{4}", po["id"])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", po["issue_date"])
    assert re.fullmatch(r"\d+\.\d{2}", po["total_net"])
    line = po["lines"][0]
    assert set(line) == {"line_no", "sku", "description", "quantity", "unit_price"}
    assert isinstance(line["line_no"], int)
    assert re.fullmatch(r"\d+", line["quantity"])
    contract = data["contracts"][0]
    assert set(contract) == {
        "id",
        "vendor_id",
        "currency",
        "valid_from",
        "valid_to",
        "prices",
        "clauses",
    }
    assert set(contract["prices"][0]) == {"sku", "unit_price"}
    assert set(contract["clauses"][0]) == {"clause_no", "title", "text"}


def test_country_list_matches_vat_rates() -> None:
    assert set(COUNTRIES) == set(_master().vat_rates)


def test_build_master_is_deterministic() -> None:
    assert master_to_json(_master()) == master_to_json(_master())
