import random
from decimal import Decimal

from synth.model import Invoice, MasterData
from synth.mutators import MUTATORS, MutationContext, MutatorNotApplicable

SEED = 20261110


def apply(name: str, master: MasterData, clean: list[Invoice]) -> tuple[Invoice, Invoice]:
    """Apply mutator `name` to the first clean invoice it accepts; return (base, mutated)."""
    ctx = MutationContext(master, random.Random(SEED), SEED, next_id=f"INV-SEED-{999:03d}")
    for base in clean:
        try:
            return base, MUTATORS[name](base, ctx)
        except MutatorNotApplicable:
            continue
    raise AssertionError(f"no clean invoice accepts {name}")


def lines_sum(inv: Invoice) -> Decimal:
    return sum((ln.line_total for ln in inv.fields.lines), Decimal(0))
