"""Command line: `python -m synth --seed N --out DIR [--force]`."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from synth import DEFAULT_SEED
from synth.generate import GeneratorError, generate, write_dataset


def _seed(value: str) -> int:
    if not value.isdigit():
        raise argparse.ArgumentTypeError(f"seed must be a non-negative integer, got {value!r}")
    return int(value)


def _out(value: str) -> Path:
    if not value.strip():
        raise argparse.ArgumentTypeError("--out must be a directory path")
    return Path(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m synth",
        description="Generate the synthetic invoice dataset (PDF + ground-truth JSON).",
    )
    parser.add_argument(
        "--seed",
        type=_seed,
        default=DEFAULT_SEED,
        help=f"random seed (default {DEFAULT_SEED}); the same seed gives byte-identical files",
    )
    parser.add_argument("--out", type=_out, required=True, help="output directory")
    parser.add_argument(
        "--force",
        action="store_true",
        help="regenerate into a non-empty directory (replaces master.json, index.json, invoices/)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        dataset = generate(args.seed)
        write_dataset(dataset, args.out, force=args.force)
    except GeneratorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    anomalous = sum(1 for i in dataset.invoices if i.anomalies)
    print(
        f"wrote {len(dataset.invoices)} invoices ({anomalous} anomalous) "
        f"with seed {args.seed} to {args.out}"
    )
    return 0
