"""Build the whole dataset from one seed and write it to disk."""

import json
import random
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from synth.invoices import build_clean_invoices, invoice_id
from synth.master import build_master
from synth.model import Invoice, MasterData, invoice_to_json, master_to_json
from synth.mutators import MUTATORS, MutationContext, MutatorNotApplicable
from synth.render import render_pdf

RESEND = "resend_reformatted_number"

# Mutators applied, one clean invoice each. The resend creates an extra invoice instead.
MUTATION_PLAN: tuple[str, ...] = tuple(MUTATORS)

OUTPUTS = ("master.json", "index.json")
INVOICES_DIR = "invoices"


class GeneratorError(Exception):
    """A user-facing error (bad output directory); the CLI prints it and exits 2."""


@dataclass(frozen=True)
class Dataset:
    seed: int
    master: MasterData
    invoices: tuple[Invoice, ...]


def generate(seed: int) -> Dataset:
    rng = random.Random(seed)
    master = build_master(rng, seed)
    invoices = build_clean_invoices(master, rng)
    order = list(range(len(invoices)))
    rng.shuffle(order)
    ctx = MutationContext(master, rng, seed, next_id=invoice_id(len(invoices) + 1))
    used: set[int] = set()
    resends: list[Invoice] = []

    for name in MUTATION_PLAN:
        for i in order:
            if i in used:
                continue
            try:
                mutated = MUTATORS[name](invoices[i], ctx)
            except MutatorNotApplicable:
                continue
            used.add(i)
            if name == RESEND:
                # The original stays clean; the duplicate arrives last.
                resends.append(mutated)
                ctx.next_id = invoice_id(len(invoices) + len(resends) + 1)
            else:
                invoices[i] = mutated
            break
        else:
            raise RuntimeError(f"no clean invoice accepts mutator {name!r} (seed {seed})")

    return Dataset(seed, master, tuple(invoices + resends))


def _dump(data: Any) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def index_json(ds: Dataset) -> dict[str, Any]:
    return {
        "seed": ds.seed,
        "invoice_count": len(ds.invoices),
        "anomalous_count": sum(1 for i in ds.invoices if i.anomalies),
        "invoices": [
            {
                "invoice_id": i.invoice_id,
                "file": i.file,
                "anomalies": list(i.anomalies),
                "expected_action": i.expected_action,
                "duplicate_of": i.duplicate_of,
            }
            for i in ds.invoices
        ],
    }


def prepare_out_dir(out: Path, force: bool) -> None:
    if out.exists() and not out.is_dir():
        raise GeneratorError(f"--out {out} exists and is not a directory")
    if out.is_dir() and any(out.iterdir()):
        if not force:
            raise GeneratorError(
                f"--out {out} is not empty; pass --force to regenerate the seed there"
            )
        # Only remove what the generator writes, so unrelated files survive.
        shutil.rmtree(out / INVOICES_DIR, ignore_errors=True)
        for name in OUTPUTS:
            (out / name).unlink(missing_ok=True)
    (out / INVOICES_DIR).mkdir(parents=True, exist_ok=True)


def write_dataset(ds: Dataset, out: Path, force: bool) -> None:
    prepare_out_dir(out, force)
    (out / "master.json").write_bytes(_dump(master_to_json(ds.master)))
    for inv in ds.invoices:
        (out / INVOICES_DIR / inv.file).write_bytes(render_pdf(inv))
        (out / INVOICES_DIR / f"{inv.invoice_id}.json").write_bytes(_dump(invoice_to_json(inv)))
    (out / "index.json").write_bytes(_dump(index_json(ds)))
