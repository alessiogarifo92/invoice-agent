"""Run every rule, fail closed: an exception in a rule becomes a `rule_error` flag.

`missing_field` flags raised by several rules for the same field are merged into one, whose
evidence lists every rule that could not run because of it.
"""

from collections.abc import Callable, Sequence

from app.rules import checks
from app.rules.context import RuleContext
from app.rules.facts import InvoiceFacts
from app.rules.flags import Flag

Rule = Callable[[InvoiceFacts, RuleContext], list[Flag]]

ERROR_MAX = 200

RULES: tuple[tuple[str, Rule], ...] = (
    ("parse_errors", checks.check_parse_errors),
    ("vendor", checks.check_vendor),
    ("totals", checks.check_totals),
    ("vat", checks.check_vat),
    ("purchase_order", checks.check_purchase_order),
    ("po_amount", checks.check_po_amount),
    ("currency", checks.check_currency),
    ("contract_prices", checks.check_contract_prices),
    ("iban", checks.check_iban),
    ("text_ibans", checks.check_text_ibans),
    ("text_values", checks.check_text_values),
    ("duplicates", checks.check_duplicates),
    ("injection", checks.check_injection),
)


def _rule_error(name: str, exc: BaseException) -> Flag:
    error = f"{type(exc).__name__}: {exc}"[:ERROR_MAX]
    return checks.flag(
        "rule_error",
        f"The {name} check failed ({error}), so its result is unknown.",
        rule=name,
        error=error,
    )


def _run_one(name: str, rule: Rule, facts: InvoiceFacts, ctx: RuleContext) -> list[Flag]:
    try:
        flags = list(rule(facts, ctx))
        bad = next((f for f in flags if not isinstance(f, Flag)), None)
        if bad is not None:
            raise TypeError(f"rule returned {type(bad).__name__}, not a Flag")
        return flags
    except Exception as exc:  # every failure must surface as a flag, never as a pass
        return [_rule_error(name, exc)]


def run_rules(
    facts: InvoiceFacts, ctx: RuleContext, rules: Sequence[tuple[str, Rule]] = RULES
) -> list[Flag]:
    """Every rule's flags, in rule order, with `missing_field` merged per field."""
    flags: list[Flag] = []
    missing: dict[str, Flag] = {}
    for name, rule in rules:
        for flag in _run_one(name, rule, facts, ctx):
            if flag.code != "missing_field":
                flags.append(flag)
                continue
            field = str(flag.evidence.get("field"))
            first = missing.get(field)
            if first is None:
                missing[field] = flag
                flags.append(flag)
                continue
            rules_seen = first.evidence.setdefault("rules", [])
            for rule_name in flag.evidence.get("rules", [name]):
                if rule_name not in rules_seen:
                    rules_seen.append(rule_name)
    return flags
