"""The rules: pure functions of (InvoiceFacts, RuleContext) that return flags.

Every rule fails closed. An input it needs that is missing or unparsable raises a
`missing_field` flag; an exception inside it becomes `rule_error` (in the runner). A rule
whose subject is already flagged by another rule skips instead of piling on: with no
resolved vendor, `check_vendor` raises `unknown_vendor`, so vendor-bound checks (IBAN
history, contract prices, duplicates) do not run; with no PO, `check_purchase_order` raises
`po_not_found`, so the PO amount checks do not run.

Evidence holds JSON-ready values only: amounts as strings with two decimals, dates as ISO.
"""

from collections import defaultdict
from decimal import Decimal
from typing import Any

from app.rules.context import RuleContext
from app.rules.facts import InvoiceFacts, LineFacts
from app.rules.flags import Action, Flag, Severity
from app.rules.iban import compact_iban, is_valid_mod97
from app.rules.numbers import CENT, fmt_money, money, normalize_invoice_number

# The flag catalogue (docs/decisions.md, "Rules"): every code with its severity and action floor.
CODES: dict[str, tuple[Severity, Action]] = {
    "total_mismatch": ("warning", "clarify"),
    "wrong_vat": ("warning", "clarify"),
    "po_not_found": ("warning", "clarify"),
    "po_vendor_mismatch": ("warning", "clarify"),
    "amount_above_tolerance": ("warning", "clarify"),
    "po_balance_exceeded": ("warning", "clarify"),
    "currency_mismatch": ("warning", "clarify"),
    "price_above_contract": ("warning", "clarify"),
    "conflicting_values_in_text": ("warning", "clarify"),
    "duplicate_suspected": ("warning", "clarify"),
    "missing_field": ("warning", "clarify"),
    "rule_error": ("warning", "clarify"),
    "iban_not_current": ("critical", "clarify"),
    "iban_in_text_not_current": ("critical", "clarify"),
    "unknown_vendor": ("critical", "clarify"),
    "injection_suspected": ("critical", "clarify"),
    "duplicate_confirmed": ("critical", "block"),
}

SNIPPETS_MAX = 3


def flag(code: str, message: str, **evidence: Any) -> Flag:
    severity, floor = CODES[code]
    return Flag(
        code=code, severity=severity, action_floor=floor, message=message, evidence=evidence
    )


def missing(facts: InvoiceFacts, field: str, rule: str) -> Flag:
    error = facts.parse_error(field)
    reason = "unparsable" if error is not None else "missing"
    evidence: dict[str, Any] = {"field": field, "reason": reason, "rules": [rule]}
    if error is not None:
        evidence["error"] = error.message
    return flag("missing_field", f"{field} is {reason}, so it cannot be checked.", **evidence)


def _differs(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) > CENT


def _lines(facts: InvoiceFacts, rule: str) -> tuple[tuple[LineFacts, ...], list[Flag]]:
    if not facts.lines:
        return (), [missing(facts, "lines", rule)]
    return facts.lines, []


def _current_ibans(ctx: RuleContext) -> set[str]:
    return {compact_iban(a.iban) for a in ctx.bank_accounts if a.valid_to is None}


def _iban_status(ctx: RuleContext, iban: str) -> str:
    known = {compact_iban(a.iban) for a in ctx.bank_accounts}
    return "past_account" if iban in known else "never_seen"


# --- rules ---------------------------------------------------------------------------------------


def check_parse_errors(facts: InvoiceFacts, ctx: RuleContext) -> list[Flag]:
    """Any extracted value that could not be read: unknown is never a pass."""
    return [missing(facts, error.field, "parse_errors") for error in facts.parse_errors]


def check_totals(facts: InvoiceFacts, ctx: RuleContext) -> list[Flag]:
    """line_total = qty x unit price, sum(lines) = subtotal, subtotal + VAT = total."""
    rule = "totals"
    lines, flags = _lines(facts, rule)
    line_totals: list[Decimal] = []
    for i, line in enumerate(lines):
        values = {"quantity": line.quantity, "unit_price": line.unit_price}
        values["line_total"] = line.line_total
        absent = [name for name, value in values.items() if value is None]
        flags += [missing(facts, f"lines[{i}].{name}", rule) for name in absent]
        if line.line_total is not None:
            line_totals.append(line.line_total)
        if line.quantity is None or line.unit_price is None or line.line_total is None:
            continue
        expected = money(line.quantity * line.unit_price)
        if _differs(expected, line.line_total):
            flags.append(
                flag(
                    "total_mismatch",
                    f"Line {line.line_no or i + 1}: {line.quantity} x {line.unit_price} is "
                    f"{expected}, not {fmt_money(line.line_total)}.",
                    check="line_total",
                    line_no=line.line_no,
                    expected=fmt_money(expected),
                    found=fmt_money(line.line_total),
                )
            )
    for name in ("subtotal", "vat_amount", "total"):
        if getattr(facts, name) is None:
            flags.append(missing(facts, name, rule))

    if lines and facts.subtotal is not None and len(line_totals) == len(lines):
        lines_sum = money(sum(line_totals, Decimal(0)))
        if _differs(lines_sum, facts.subtotal):
            flags.append(
                flag(
                    "total_mismatch",
                    f"The lines add up to {lines_sum}, the subtotal says "
                    f"{fmt_money(facts.subtotal)}.",
                    check="sum_of_lines",
                    expected=fmt_money(lines_sum),
                    found=fmt_money(facts.subtotal),
                )
            )
    if facts.subtotal is not None and facts.vat_amount is not None and facts.total is not None:
        expected = money(facts.subtotal + facts.vat_amount)
        if _differs(expected, facts.total):
            flags.append(
                flag(
                    "total_mismatch",
                    f"Subtotal {fmt_money(facts.subtotal)} plus VAT {fmt_money(facts.vat_amount)} "
                    f"is {expected}, the total says {fmt_money(facts.total)}.",
                    check="subtotal_plus_vat",
                    expected=fmt_money(expected),
                    found=fmt_money(facts.total),
                )
            )
    return flags


def check_vat(facts: InvoiceFacts, ctx: RuleContext) -> list[Flag]:
    """The VAT rate of the vendor's country, and VAT amount = subtotal x printed rate."""
    rule = "vat"
    flags: list[Flag] = []
    vendor = ctx.vendor
    # Master data is the reference for a known vendor; the printed country only otherwise.
    country = vendor.country if vendor is not None else facts.vendor_country
    expected_rate: Decimal | None = None
    if country is None:
        flags.append(missing(facts, "vendor_country", rule))
    else:
        expected_rate = ctx.vat_rates.get(country.upper())
        if expected_rate is None:
            flags.append(missing(facts, f"vat_rates[{country.upper()}]", rule))
    for name in ("vat_rate", "vat_amount", "subtotal"):
        if getattr(facts, name) is None:
            flags.append(missing(facts, name, rule))

    if facts.vat_rate is not None and expected_rate is not None and country is not None:
        if facts.vat_rate.compare(expected_rate) != 0:
            flags.append(
                flag(
                    "wrong_vat",
                    f"VAT is charged at {fmt_money(facts.vat_rate)}%; the rate for {country} is "
                    f"{fmt_money(expected_rate)}%.",
                    check="rate",
                    country=country,
                    expected_rate=fmt_money(expected_rate),
                    found_rate=fmt_money(facts.vat_rate),
                )
            )
    if facts.vat_rate is not None and facts.vat_amount is not None and facts.subtotal is not None:
        expected = money(facts.subtotal * facts.vat_rate / 100)
        if _differs(expected, facts.vat_amount):
            flags.append(
                flag(
                    "wrong_vat",
                    f"VAT at {fmt_money(facts.vat_rate)}% of {fmt_money(facts.subtotal)} is "
                    f"{expected}, the invoice says {fmt_money(facts.vat_amount)}.",
                    check="amount",
                    expected=fmt_money(expected),
                    found=fmt_money(facts.vat_amount),
                )
            )
    return flags


def check_purchase_order(facts: InvoiceFacts, ctx: RuleContext) -> list[Flag]:
    """The PO exists and belongs to this vendor."""
    if facts.po_number is None:
        return [missing(facts, "po_number", "purchase_order")]
    po = ctx.po
    if po is None:
        return [
            flag(
                "po_not_found",
                f"Purchase order {facts.po_number} is not in master data.",
                po_number=facts.po_number,
            )
        ]
    vendor = ctx.vendor
    if vendor is not None and po.vendor_id != vendor.id:
        return [
            flag(
                "po_vendor_mismatch",
                f"Purchase order {po.id} belongs to vendor {po.vendor_id}, not to "
                f"{vendor.id} ({vendor.name}).",
                po_id=po.id,
                po_vendor_id=po.vendor_id,
                invoice_vendor_id=vendor.id,
            )
        ]
    return []


def check_po_amount(facts: InvoiceFacts, ctx: RuleContext) -> list[Flag]:
    """Net amount within the PO tolerance, and cumulative billing within the PO total."""
    rule = "po_amount"
    po = ctx.po
    if po is None:
        return []
    flags: list[Flag] = [
        missing(facts, f"billed_before[{i}]", rule) for i in po.billed_unreadable_ids
    ]
    if facts.subtotal is None:
        flags.append(missing(facts, "subtotal", rule))
    if ctx.po_tolerance_pct is None:
        flags.append(missing(facts, "po_tolerance_pct", rule))
    if facts.subtotal is None or ctx.po_tolerance_pct is None:
        return flags

    tolerance = ctx.po_tolerance_pct
    limit = money(po.total_net * (1 + tolerance / 100))
    if facts.subtotal > limit:
        over_pct = money((facts.subtotal / po.total_net - 1) * 100) if po.total_net else None
        flags.append(
            flag(
                "amount_above_tolerance",
                f"Net {fmt_money(facts.subtotal)} is above {po.id}'s {fmt_money(po.total_net)} "
                f"plus the {fmt_money(tolerance)}% tolerance ({limit}).",
                po_id=po.id,
                invoice_net=fmt_money(facts.subtotal),
                po_total_net=fmt_money(po.total_net),
                tolerance_pct=fmt_money(tolerance),
                limit=fmt_money(limit),
                over_pct=fmt_money(over_pct) if over_pct is not None else None,
            )
        )
    cumulative = po.billed_before + facts.subtotal
    if po.billed_before > 0 and cumulative > limit:
        flags.append(
            flag(
                "po_balance_exceeded",
                f"{po.id} already has {fmt_money(po.billed_before)} billed; with this invoice "
                f"the total {fmt_money(cumulative)} exceeds {limit}.",
                po_id=po.id,
                billed_before=fmt_money(po.billed_before),
                billed_invoice_ids=list(po.billed_invoice_ids),
                invoice_net=fmt_money(facts.subtotal),
                cumulative=fmt_money(cumulative),
                limit=fmt_money(limit),
            )
        )
    return flags


def check_currency(facts: InvoiceFacts, ctx: RuleContext) -> list[Flag]:
    """The invoice currency is the PO's and the contract's."""
    if facts.currency is None:
        return [missing(facts, "currency", "currency")]
    currency = facts.currency.upper()
    flags: list[Flag] = []
    if ctx.po is not None and ctx.po.currency.upper() != currency:
        flags.append(
            flag(
                "currency_mismatch",
                f"Invoice in {currency}, purchase order {ctx.po.id} in {ctx.po.currency}.",
                against="purchase_order",
                reference=ctx.po.id,
                invoice_currency=currency,
                expected_currency=ctx.po.currency,
            )
        )
    by_contract = {p.contract_id: p.currency for p in ctx.contract_prices}
    for contract_id, contract_currency in sorted(by_contract.items()):
        if contract_currency.upper() != currency:
            flags.append(
                flag(
                    "currency_mismatch",
                    f"Invoice in {currency}, contract {contract_id} in {contract_currency}.",
                    against="contract",
                    reference=contract_id,
                    invoice_currency=currency,
                    expected_currency=contract_currency,
                )
            )
    return flags


def check_contract_prices(facts: InvoiceFacts, ctx: RuleContext) -> list[Flag]:
    """Each line's unit price is at most the contract price for its SKU (plus a cent)."""
    rule = "contract_prices"
    if ctx.vendor is None:
        return []
    lines, flags = _lines(facts, rule)
    if facts.issue_date is None:
        # The contract in force is chosen by issue date.
        return [*flags, missing(facts, "issue_date", rule)]
    prices: dict[str, tuple[Decimal, str]] = {}
    for price in ctx.contract_prices:
        known = prices.get(price.sku)
        if known is None or price.unit_price < known[0]:
            prices[price.sku] = (price.unit_price, price.contract_id)
    for i, line in enumerate(lines):
        if line.sku is None:
            flags.append(missing(facts, f"lines[{i}].sku", rule))
            continue
        if line.unit_price is None:
            flags.append(missing(facts, f"lines[{i}].unit_price", rule))
            continue
        if line.sku not in prices:
            flags.append(missing(facts, f"contract_price[{line.sku}]", rule))
            continue
        contract_price, contract_id = prices[line.sku]
        if line.unit_price - contract_price > CENT:
            flags.append(
                flag(
                    "price_above_contract",
                    f"{line.sku} is billed at {fmt_money(line.unit_price)}; contract "
                    f"{contract_id} says {fmt_money(contract_price)}.",
                    sku=line.sku,
                    line_no=line.line_no,
                    unit_price=fmt_money(line.unit_price),
                    contract_price=fmt_money(contract_price),
                    contract_id=contract_id,
                )
            )
    return flags


def check_iban(facts: InvoiceFacts, ctx: RuleContext) -> list[Flag]:
    """The IBAN to pay is the vendor's current account in master data."""
    if facts.iban is None:
        return [missing(facts, "iban", "iban")]
    if ctx.vendor is None:
        return []
    iban = compact_iban(facts.iban)
    current = _current_ibans(ctx)
    if iban in current:
        return []
    status = _iban_status(ctx, iban)
    why = "a closed past account" if status == "past_account" else "never registered"
    return [
        flag(
            "iban_not_current",
            f"The invoice asks for payment to {iban}, {why} for {ctx.vendor.name}.",
            iban=iban,
            status=status,
            mod97_valid=is_valid_mod97(iban),
            current_iban_on_file=bool(current),
        )
    ]


def check_text_ibans(facts: InvoiceFacts, ctx: RuleContext) -> list[Flag]:
    """Every other IBAN found anywhere in the PDF, hidden text and metadata included.

    The extracted IBAN itself is `check_iban`'s subject, so it is not flagged twice here.
    """
    current = _current_ibans(ctx)
    extracted = compact_iban(facts.iban) if facts.iban is not None else None
    sources: dict[str, list[str]] = defaultdict(list)
    for found in ctx.text_sweep.ibans:
        if found.value in current or found.value == extracted:
            continue
        if found.source not in sources[found.value]:
            sources[found.value].append(found.source)
    return [
        flag(
            "iban_in_text_not_current",
            f"The document also mentions IBAN {iban} ({', '.join(where)}), which is not the "
            "vendor's current account.",
            iban=iban,
            sources=where,
            status=_iban_status(ctx, iban),
        )
        for iban, where in sources.items()
    ]


def check_text_values(facts: InvoiceFacts, ctx: RuleContext) -> list[Flag]:
    """Every invoice number and labelled total found in the PDF agrees with the extracted one."""
    rule = "text_values"
    sweep = ctx.text_sweep
    flags: list[Flag] = []
    if sweep.invoice_numbers and facts.invoice_number is None:
        flags.append(missing(facts, "invoice_number", rule))
    elif facts.invoice_number is not None:
        extracted = normalize_invoice_number(facts.invoice_number)
        numbers: dict[str, tuple[str, list[str]]] = {}
        for found in sweep.invoice_numbers:
            norm = normalize_invoice_number(found.value)
            if norm == extracted:
                continue
            raw, where = numbers.setdefault(norm, (found.value, []))
            if found.source not in where:
                where.append(found.source)
        flags += [
            flag(
                "conflicting_values_in_text",
                f"The document also names invoice number {raw} ({', '.join(where)}), not "
                f"{facts.invoice_number}.",
                field="invoice_number",
                extracted=facts.invoice_number,
                found=raw,
                sources=where,
            )
            for raw, where in numbers.values()
        ]
    if sweep.totals and facts.total is None:
        flags.append(missing(facts, "total", rule))
    elif facts.total is not None:
        totals: dict[Decimal, list[str]] = {}
        for amount in sweep.totals:
            if not _differs(amount.value, facts.total):
                continue
            where = totals.setdefault(money(amount.value), [])
            if amount.source not in where:
                where.append(amount.source)
        flags += [
            flag(
                "conflicting_values_in_text",
                f"The document also states a total of {value} ({', '.join(where)}), not "
                f"{fmt_money(facts.total)}.",
                field="total",
                extracted=fmt_money(facts.total),
                found=fmt_money(value),
                sources=where,
            )
            for value, where in totals.items()
        ]
    return flags


def check_vendor(facts: InvoiceFacts, ctx: RuleContext) -> list[Flag]:
    """The vendor matched master data by VAT id, IBAN or a confident name match."""
    if ctx.vendor_match is not None:
        return []
    return [
        flag(
            "unknown_vendor",
            "No vendor in master data matches this invoice's VAT id, IBAN or name.",
            vat_id=facts.vendor_vat_id,
            iban=compact_iban(facts.iban) if facts.iban is not None else None,
            name=facts.vendor_name,
        )
    ]


def check_duplicates(facts: InvoiceFacts, ctx: RuleContext) -> list[Flag]:
    """Earlier invoices of this vendor: same normalised number blocks, a near match clarifies."""
    norm = normalize_invoice_number(facts.invoice_number or "")
    if not norm:
        # No letters or digits to compare: an empty key would match every other empty one.
        return [missing(facts, "invoice_number", "duplicates")]
    if ctx.vendor is None:
        return []
    flags = []
    for candidate in ctx.duplicates:
        # Decided on the numbers themselves, not on the matcher's label.
        confirmed = candidate.invoice_number_norm == norm
        code = "duplicate_confirmed" if confirmed else "duplicate_suspected"
        what = (
            "the same invoice number"
            if confirmed
            else "a similar number, the same amount and a close date"
        )
        flags.append(
            flag(
                code,
                f"Invoice {candidate.invoice_id} ({candidate.invoice_number}) from this vendor "
                f"has {what}.",
                invoice_id=candidate.invoice_id,
                invoice_number=candidate.invoice_number,
                invoice_number_norm=candidate.invoice_number_norm,
                total=fmt_money(candidate.total) if candidate.total is not None else None,
                issue_date=candidate.issue_date.isoformat() if candidate.issue_date else None,
                similarity=round(candidate.similarity, 3),
            )
        )
    return flags


def check_injection(facts: InvoiceFacts, ctx: RuleContext) -> list[Flag]:
    """Hidden text and instruction-like text, one flag per kind of finding."""
    by_kind: dict[str, list[Any]] = defaultdict(list)
    for finding in ctx.document_findings:
        by_kind[finding.kind].append(finding)
    flags = []
    for kind, findings in by_kind.items():
        patterns = sorted({p for f in findings for p in f.evidence.get("patterns", [])})
        pages = sorted({f.page for f in findings if f.page is not None})
        flags.append(
            flag(
                "injection_suspected",
                f"{len(findings)} {kind.replace('_', ' ')} finding(s) in the document: text a "
                "reader does not see, or that addresses the reviewing system.",
                kind=kind,
                count=len(findings),
                pages=pages,
                patterns=patterns,
                snippets=[f.snippet for f in findings[:SNIPPETS_MAX]],
            )
        )
    return flags
