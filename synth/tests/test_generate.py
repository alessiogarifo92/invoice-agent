import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from synth import DEFAULT_SEED
from synth.generate import Dataset, generate, write_dataset
from synth.iban import FAKE_BANK_CODES, KNOWN_TEST_IBANS, bank_code, is_valid_mod97
from synth.mutators import MUTATORS, attacker_iban_of

SYNTH_DIR = Path(__file__).resolve().parents[1]
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")


@pytest.fixture(scope="module")
def dataset() -> Dataset:
    return generate(DEFAULT_SEED)


def _hashes(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_default_seed_is_the_documented_one() -> None:
    assert DEFAULT_SEED == 20261110


def test_about_sixty_invoices_with_ten_to_twenty_percent_anomalous(dataset: Dataset) -> None:
    n = len(dataset.invoices)
    assert 55 <= n <= 70
    anomalous = [i for i in dataset.invoices if i.anomalies]
    assert 0.10 <= len(anomalous) / n <= 0.20


def test_every_mutator_is_used_at_least_once(dataset: Dataset) -> None:
    used = Counter(a for inv in dataset.invoices for a in inv.anomalies)
    assert set(used) == set(MUTATORS)


def test_invoice_ids_are_unique_and_sequential(dataset: Dataset) -> None:
    ids = [i.invoice_id for i in dataset.invoices]
    assert ids == [f"INV-SEED-{n:03d}" for n in range(1, len(ids) + 1)]


def test_duplicates_point_to_a_clean_original(dataset: Dataset) -> None:
    by_id = {i.invoice_id: i for i in dataset.invoices}
    dups = [i for i in dataset.invoices if i.duplicate_of]
    assert dups
    for dup in dups:
        assert dup.anomalies == ("resend_reformatted_number",)
        assert dup.expected_action == "block"
        original = by_id[dup.duplicate_of or ""]
        assert original.anomalies == ()
        assert original.fields.total == dup.fields.total
    assert all(i.duplicate_of is None for i in dataset.invoices if not i.anomalies)


def test_expected_actions(dataset: Dataset) -> None:
    for inv in dataset.invoices:
        if not inv.anomalies:
            assert inv.expected_action == "approve"
        elif "resend_reformatted_number" in inv.anomalies:
            assert inv.expected_action == "block"
        else:
            assert inv.expected_action == "clarify"


def test_every_iban_passes_mod97_with_a_made_up_bank_code(dataset: Dataset) -> None:
    ibans = [a.iban for a in dataset.master.bank_accounts]
    ibans += [i.fields.iban for i in dataset.invoices]
    ibans += [x for i in dataset.invoices if (x := attacker_iban_of(i))]
    assert len(ibans) > len(dataset.invoices)
    for iban in ibans:
        assert is_valid_mod97(iban), iban
        assert bank_code(iban) in FAKE_BANK_CODES, iban
        assert iban not in KNOWN_TEST_IBANS, iban


def test_written_output_follows_the_contract(dataset: Dataset, tmp_path: Path) -> None:
    write_dataset(dataset, tmp_path, force=False)
    master = json.loads((tmp_path / "master.json").read_text(encoding="utf-8"))
    assert len(master["vendors"]) == len(dataset.master.vendors)
    index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert index["seed"] == DEFAULT_SEED
    assert [e["invoice_id"] for e in index["invoices"]] == [i.invoice_id for i in dataset.invoices]
    for entry in index["invoices"]:
        assert set(entry) == {"invoice_id", "file", "anomalies", "expected_action", "duplicate_of"}
        pdf = tmp_path / "invoices" / entry["file"]
        assert pdf.read_bytes().startswith(b"%PDF-")
        assert pdf.stat().st_size < 40_000
        truth = json.loads((tmp_path / "invoices" / f"{entry['invoice_id']}.json").read_text())
        assert truth["anomalies"] == entry["anomalies"]
        assert truth["expected_action"] == entry["expected_action"]
        assert truth["duplicate_of"] == entry["duplicate_of"]
    files = sorted(p.name for p in (tmp_path / "invoices").iterdir())
    assert len(files) == 2 * len(dataset.invoices)


def test_every_iban_in_the_written_pdfs_is_synthetic(dataset: Dataset, tmp_path: Path) -> None:
    import pdfplumber

    write_dataset(dataset, tmp_path, force=False)
    for pdf_path in sorted((tmp_path / "invoices").glob("*.pdf")):
        with pdfplumber.open(pdf_path) as doc:
            text = "\n".join(p.extract_text() for p in doc.pages)
            text += " " + " ".join(str(v) for v in doc.metadata.values())
        found = IBAN_RE.findall(text)
        assert found, pdf_path.name
        for iban in found:
            assert is_valid_mod97(iban) and bank_code(iban) in FAKE_BANK_CODES, (pdf_path, iban)


def test_a_different_seed_gives_a_different_dataset(dataset: Dataset) -> None:
    other = generate(DEFAULT_SEED + 1)
    assert other.invoices[0].fields != dataset.invoices[0].fields


def _run_cli(out: Path, hash_seed: str) -> None:
    env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    subprocess.run(
        [sys.executable, "-m", "synth", "--seed", str(DEFAULT_SEED), "--out", str(out)],
        cwd=SYNTH_DIR,
        env=env,
        check=True,
        capture_output=True,
    )


def test_same_seed_twice_gives_byte_identical_files(tmp_path: Path) -> None:
    # Separate processes with different hash seeds: set/dict iteration cannot leak into output.
    first, second = tmp_path / "a", tmp_path / "b"
    _run_cli(first, "1")
    _run_cli(second, "2")
    hashes = _hashes(first)
    assert len(hashes) > 100
    assert any(name.endswith(".pdf") for name in hashes)
    assert hashes == _hashes(second)


def test_committed_seed_matches_a_fresh_generation(tmp_path: Path) -> None:
    committed = SYNTH_DIR.parent / "data" / "seed"
    if not committed.is_dir():
        pytest.skip("data/seed not generated in this checkout")
    _run_cli(tmp_path, "0")
    assert _hashes(tmp_path) == _hashes(committed), "data/seed is stale: run `make seed`"
