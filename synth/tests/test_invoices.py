from decimal import Decimal

from synth.iban import is_valid_mod97
from synth.invoices import normalize_invoice_number, reformat_invoice_number
from synth.model import Invoice, MasterData, invoice_to_json
from tests.helpers import lines_sum


def test_clean_invoices_match_po_contract_and_master(
    master: MasterData, clean: list[Invoice]
) -> None:
    assert len(clean) == len(master.purchase_orders)
    for inv in clean:
        f = inv.fields
        po = master.purchase_order(f.po_number)
        vendor = master.vendor(po.vendor_id)
        contract = master.contract_for(vendor.id)
        assert (f.vendor_name, f.vendor_vat_id, f.vendor_country, f.vendor_address) == (
            vendor.name,
            vendor.vat_id,
            vendor.country,
            vendor.address,
        )
        assert f.iban == master.current_iban(vendor.id)
        assert is_valid_mod97(f.iban)
        assert [(ln.sku, ln.quantity, ln.unit_price) for ln in f.lines] == [
            (ln.sku, ln.quantity, ln.unit_price) for ln in po.lines
        ]
        assert f.subtotal == lines_sum(inv) == po.total_net
        assert f.vat_rate == master.vat_rates[vendor.country]
        assert f.vat_amount == (f.subtotal * f.vat_rate / 100).quantize(Decimal("0.01"))
        assert f.total == f.subtotal + f.vat_amount
        assert po.issue_date < f.issue_date < f.due_date
        assert (f.due_date - f.issue_date).days == contract.payment_days
        assert inv.anomalies == ()
        assert inv.expected_action == "approve"


def test_invoice_ids_and_vendor_numbers_are_unique(clean: list[Invoice]) -> None:
    ids = [i.invoice_id for i in clean]
    assert ids == [f"INV-SEED-{n:03d}" for n in range(1, len(clean) + 1)]
    keys = [(i.fields.vendor_vat_id, i.fields.invoice_number) for i in clean]
    assert len(set(keys)) == len(keys)


def test_invoice_json_follows_the_output_contract(clean: list[Invoice]) -> None:
    data = invoice_to_json(clean[0])
    assert list(data) == [
        "invoice_id",
        "file",
        "fields",
        "anomalies",
        "expected_action",
        "duplicate_of",
        "notes",
    ]
    assert data["file"] == f"{data['invoice_id']}.pdf"
    assert list(data["fields"]) == [
        "vendor_name",
        "vendor_vat_id",
        "vendor_country",
        "vendor_address",
        "invoice_number",
        "issue_date",
        "due_date",
        "currency",
        "po_number",
        "iban",
        "lines",
        "subtotal",
        "vat_rate",
        "vat_amount",
        "total",
    ]
    assert list(data["fields"]["lines"][0]) == [
        "line_no",
        "sku",
        "description",
        "quantity",
        "unit_price",
        "line_total",
    ]
    assert data["anomalies"] == []
    assert data["duplicate_of"] is None


def test_reformatted_number_differs_but_normalises_to_the_same_key() -> None:
    assert reformat_invoice_number("INV-0042") == "INV 42"
    for number in ("INV-0042", "2026/017", "F-2026-0042", "RE-10042", "FAC-2026-007"):
        other = reformat_invoice_number(number)
        assert other != number
        assert normalize_invoice_number(other) == normalize_invoice_number(number)
