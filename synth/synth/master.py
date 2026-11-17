"""Master data: vendors, IBAN history, contracts (prices + clauses) and purchase orders."""

import random
from datetime import date, timedelta
from decimal import Decimal

from faker import Faker
from faker.providers.address.it_IT import Provider as ItalianAddress

from synth.iban import make_iban
from synth.model import (
    BankAccount,
    Clause,
    Contract,
    ContractPrice,
    MasterData,
    POLine,
    PurchaseOrder,
    Vendor,
)

COUNTRIES: dict[str, tuple[str, str, str]] = {
    # country: (Faker locale, country name, legal form)
    "IT": ("it_IT", "Italy", "S.r.l."),
    "DE": ("de_DE", "Germany", "GmbH"),
    "FR": ("fr_FR", "France", "SARL"),
    "ES": ("es_ES", "Spain", "S.L."),
    "NL": ("nl_NL", "Netherlands", "B.V."),
}

VAT_RATES: dict[str, Decimal] = {
    "IT": Decimal("22.00"),
    "DE": Decimal("19.00"),
    "FR": Decimal("20.00"),
    "ES": Decimal("21.00"),
    "NL": Decimal("21.00"),
}

PO_TOLERANCE_PCT = Decimal("2.00")

# Every vendor name carries this word, so no name can be mistaken for a real company.
FICTIONAL_MARK = "Synthetica"

# Vendor countries in order (12 vendors, every country at least twice).
VENDOR_COUNTRIES: tuple[str, ...] = (
    "IT",
    "DE",
    "FR",
    "ES",
    "NL",
    "IT",
    "DE",
    "FR",
    "ES",
    "NL",
    "IT",
    "DE",
)

# Digits after the country prefix of a synthetic VAT id; every id starts with "99".
_VAT_DIGITS: dict[str, int] = {"IT": 11, "DE": 9, "FR": 11, "ES": 9, "NL": 9}

# (sector, [(description, base unit price in cents, min qty, max qty)])
CATALOG: tuple[tuple[str, tuple[tuple[str, int, int, int], ...]], ...] = (
    (
        "Packaging",
        (
            ("Corrugated box 40x30x20 cm", 185, 100, 800),
            ("Pallet stretch film 23 micron", 2490, 10, 60),
            ("Packing tape 50 mm x 66 m", 245, 50, 400),
            ("Bubble wrap roll 1 m x 100 m", 3890, 5, 30),
        ),
    ),
    (
        "Office Supplies",
        (
            ("A4 copy paper 80 g, 500 sheets", 525, 50, 400),
            ("Toner cartridge, black", 8900, 2, 20),
            ("Ballpoint pens, box of 50", 1450, 2, 30),
            ("Archive box, 80 mm spine", 310, 20, 200),
        ),
    ),
    (
        "Fasteners",
        (
            ("Hex bolt M8 x 40, box of 100", 1290, 5, 80),
            ("Stainless washer M8, box of 200", 760, 5, 80),
            ("Wall anchor 10 mm, box of 50", 980, 5, 60),
            ("Threaded rod M10 x 1 m", 340, 10, 150),
        ),
    ),
    (
        "Cleaning",
        (
            ("Floor detergent, 10 L can", 2350, 4, 40),
            ("Nitrile gloves, box of 100", 890, 10, 120),
            ("Paper towel roll, pack of 6", 1190, 10, 100),
            ("Microfibre cloth, pack of 10", 1590, 5, 50),
        ),
    ),
    (
        "IT Hardware",
        (
            ("USB-C docking station", 14900, 2, 20),
            ("24 inch monitor", 18900, 2, 15),
            ("Wired keyboard, US layout", 2900, 5, 40),
            ("Cat6 patch cable 2 m", 450, 20, 200),
        ),
    ),
    (
        "Safety Equipment",
        (
            ("Safety helmet, white", 1650, 5, 60),
            ("High-visibility vest, size L", 690, 10, 120),
            ("Safety goggles, anti-fog", 980, 10, 100),
            ("Ear defenders, 30 dB", 2190, 5, 40),
        ),
    ),
    (
        "Logistics",
        (
            ("Pallet transport, per pallet", 6500, 2, 40),
            ("Warehousing, per pallet-week", 850, 20, 200),
            ("Express parcel, up to 5 kg", 1490, 5, 80),
            ("Handling fee, per order", 2500, 1, 20),
        ),
    ),
    (
        "Electrical",
        (
            ("LED panel 60x60 cm, 36 W", 3990, 5, 60),
            ("Installation cable 3G1.5, 100 m", 7900, 2, 20),
            ("Circuit breaker C16, 1 pole", 690, 10, 100),
            ("Cable ties 300 mm, bag of 100", 520, 10, 120),
        ),
    ),
    (
        "Catering",
        (
            ("Coffee beans, 1 kg bag", 1890, 10, 80),
            ("Paper cups 200 ml, pack of 100", 640, 10, 120),
            ("Bottled water 0.5 L, crate of 24", 790, 10, 100),
            ("Tea bags, box of 100", 520, 5, 60),
        ),
    ),
    (
        "Lab Supplies",
        (
            ("Pipette tips 200 ul, rack of 96", 870, 10, 100),
            ("Sample tube 15 ml, bag of 50", 1150, 10, 80),
            ("Lab coat, size M", 2450, 2, 30),
            ("Disinfectant spray, 1 L", 680, 10, 80),
        ),
    ),
    (
        "Printing",
        (
            ("Business cards, box of 500", 2900, 2, 20),
            ("Flyers A5, pack of 1000", 9500, 1, 10),
            ("Roll-up banner 85x200 cm", 7900, 1, 6),
            ("Letterhead paper, pack of 500", 4200, 1, 12),
        ),
    ),
    (
        "Facility Services",
        (
            ("Office cleaning, per visit", 18500, 2, 12),
            ("Window cleaning, per floor", 9500, 1, 8),
            ("HVAC filter replacement", 4500, 2, 20),
            ("Pest control inspection", 12000, 1, 4),
        ),
    ),
)

PO_COUNT = 62
CONTRACT_FROM = date(2025, 1, 1)
CONTRACT_TO = date(2026, 12, 31)
IBAN_START = date(2024, 1, 1)
IBAN_CHANGE = date(2025, 10, 1)
PO_FIRST_DAY = date(2026, 1, 5)
PO_LAST_DAY = date(2026, 9, 30)


def _cp1252_safe(text: str) -> bool:
    # The PDFs use the standard Helvetica font (WinAnsi encoding).
    try:
        text.encode("cp1252")
    except UnicodeEncodeError:
        return False
    return True


def fakers(seed: int) -> dict[str, Faker]:
    out: dict[str, Faker] = {}
    for offset, (country, (locale, _, _)) in enumerate(COUNTRIES.items()):
        fk = Faker(locale)
        fk.seed_instance(seed + offset)
        out[country] = fk
    return out


# Faker builds its it_IT city list from a set, so its order follows PYTHONHASHSEED.
_IT_CITIES: tuple[str, ...] = tuple(sorted(ItalianAddress.cities))


def _city(fk: Faker, country: str) -> str:
    if country == "IT":
        return str(fk.random_element(_IT_CITIES))
    return fk.city()


def fake_address(fk: Faker, country: str) -> str:
    while True:
        street = fk.street_address().replace("\n", " ")
        text = f"{street}, {fk.postcode()} {_city(fk, country)}, {COUNTRIES[country][1]}"
        if _cp1252_safe(text) and len(text) <= 80:
            return text


def fake_vendor_name(fk: Faker, country: str, sector: str, taken: set[str]) -> str:
    while True:
        name = f"{fk.last_name()} {sector} {FICTIONAL_MARK} {COUNTRIES[country][2]}"
        if _cp1252_safe(name) and name not in taken and len(name) <= 60:
            return name


def fake_vat_id(country: str, rng: random.Random) -> str:
    digits = "99" + "".join(rng.choice("0123456789") for _ in range(_VAT_DIGITS[country] - 2))
    return f"{country}{digits}B01" if country == "NL" else f"{country}{digits}"


def _clauses(payment_days: int, rng: random.Random) -> tuple[Clause, ...]:
    adjust_cap = rng.choice((2, 3, 4))
    penalty = rng.choice(("0.5", "1.0", "1.5"))
    penalty_cap = rng.choice((5, 8, 10))
    return (
        Clause(
            "3.1",
            "Prices",
            "Unit prices are those listed in Annex A for each SKU and are fixed for the "
            "contract term unless amended under clause 4.2.",
        ),
        Clause(
            "4.1",
            "Payment terms",
            f"Payment is due {payment_days} days from the invoice date by bank transfer to the "
            "account registered in the buyer's vendor master data.",
        ),
        Clause(
            "4.2",
            "Price adjustment",
            f"Prices may be adjusted at most once per calendar year, by no more than "
            f"{adjust_cap}%, and only by a written amendment signed by both parties 60 days in "
            "advance. Invoices at prices above Annex A without such an amendment are disputed.",
        ),
        Clause(
            "4.3",
            "Invoicing requirements",
            "Each invoice must quote the purchase order number and the vendor VAT id. Invoiced "
            f"amounts may not exceed the purchase order net value by more than "
            f"{PO_TOLERANCE_PCT}% without prior written approval.",
        ),
        Clause(
            "5.1",
            "Change of bank details",
            "Any change of bank details must be notified in writing and confirmed by a call-back "
            "to a known contact before the first payment to the new account. Bank details "
            "stated only on an invoice are never sufficient.",
        ),
        Clause(
            "6.1",
            "Late delivery penalty",
            f"For each full week of delay beyond the agreed delivery date the vendor owes a "
            f"penalty of {penalty}% of the net value of the late goods, capped at "
            f"{penalty_cap}%.",
        ),
        Clause(
            "9.1",
            "Termination",
            "Either party may terminate this agreement with 90 days' written notice.",
        ),
    )


def build_master(rng: random.Random, seed: int, po_count: int = PO_COUNT) -> MasterData:
    fks = fakers(seed)
    vendors: list[Vendor] = []
    accounts: list[BankAccount] = []
    contracts: list[Contract] = []
    names: set[str] = set()
    vat_ids: set[str] = set()
    sku_no = 0
    catalog_by_vendor: dict[str, list[tuple[str, str, int, int]]] = {}

    with_history = set(rng.sample(range(len(VENDOR_COUNTRIES)), 4))
    for idx, country in enumerate(VENDOR_COUNTRIES):
        vendor_id = f"V{idx + 1:03d}"
        sector, products = CATALOG[idx]
        name = fake_vendor_name(fks[country], country, sector, names)
        names.add(name)
        vat_id = fake_vat_id(country, rng)
        while vat_id in vat_ids:
            vat_id = fake_vat_id(country, rng)
        vat_ids.add(vat_id)
        vendors.append(
            Vendor(vendor_id, name, vat_id, country, fake_address(fks[country], country))
        )

        if idx in with_history:
            accounts.append(
                BankAccount(
                    vendor_id,
                    make_iban(country, rng),
                    IBAN_START,
                    IBAN_CHANGE - timedelta(days=1),
                )
            )
            accounts.append(BankAccount(vendor_id, make_iban(country, rng), IBAN_CHANGE, None))
        else:
            accounts.append(BankAccount(vendor_id, make_iban(country, rng), IBAN_START, None))

        prices: list[ContractPrice] = []
        catalog_by_vendor[vendor_id] = []
        for description, base_cents, qmin, qmax in products:
            sku_no += 1
            sku = f"SKU-{sku_no:03d}"
            cents = round(base_cents * rng.uniform(0.9, 1.1))
            prices.append(ContractPrice(sku, Decimal(cents) / 100))
            catalog_by_vendor[vendor_id].append((sku, description, qmin, qmax))
        payment_days = rng.choice((30, 45, 60))
        contracts.append(
            Contract(
                f"C{idx + 1:03d}",
                vendor_id,
                "EUR",
                CONTRACT_FROM,
                CONTRACT_TO,
                payment_days,
                tuple(prices),
                _clauses(payment_days, rng),
            )
        )

    span = (PO_LAST_DAY - PO_FIRST_DAY).days
    issue_dates = sorted(
        PO_FIRST_DAY + timedelta(days=rng.randint(0, span)) for _ in range(po_count)
    )
    vendor_order = [vendors[i % len(vendors)].id for i in range(po_count)]
    rng.shuffle(vendor_order)
    pos: list[PurchaseOrder] = []
    for n, (vendor_id, issue_date) in enumerate(zip(vendor_order, issue_dates, strict=True)):
        contract = contracts[int(vendor_id[1:]) - 1]
        items = rng.sample(catalog_by_vendor[vendor_id], rng.randint(1, 4))
        lines = tuple(
            POLine(
                line_no,
                sku,
                description,
                rng.randint(qmin, qmax),
                contract.price_of(sku),
            )
            for line_no, (sku, description, qmin, qmax) in enumerate(items, start=1)
        )
        pos.append(PurchaseOrder(f"PO-2026-{n + 1:04d}", vendor_id, "EUR", issue_date, lines))

    return MasterData(
        vendors=tuple(vendors),
        bank_accounts=tuple(accounts),
        purchase_orders=tuple(pos),
        contracts=tuple(contracts),
        vat_rates=dict(VAT_RATES),
        po_tolerance_pct=PO_TOLERANCE_PCT,
    )
