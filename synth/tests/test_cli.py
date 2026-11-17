from pathlib import Path

import pytest

from synth.cli import main


def test_writes_the_dataset(tmp_path: Path) -> None:
    out = tmp_path / "seed"
    assert main(["--seed", "7", "--out", str(out)]) == 0
    assert (out / "master.json").is_file()
    assert (out / "index.json").is_file()
    assert any((out / "invoices").glob("*.pdf"))


@pytest.mark.parametrize("seed", ["abc", "-1", "1.5", ""])
def test_invalid_seed_is_a_clear_error(
    seed: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--seed", seed, "--out", str(tmp_path / "x")])
    assert exc.value.code == 2
    assert "seed must be a non-negative integer" in capsys.readouterr().err
    assert not (tmp_path / "x").exists()


def test_out_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--seed", "1"])
    assert exc.value.code == 2
    assert "--out" in capsys.readouterr().err


def test_out_pointing_to_a_file_is_a_clear_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "file.txt"
    target.write_text("keep me")
    assert main(["--out", str(target)]) == 2
    assert "is not a directory" in capsys.readouterr().err
    assert target.read_text() == "keep me"


def test_refuses_a_non_empty_directory_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "other.txt").write_text("keep me")
    assert main(["--out", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "not empty" in err and "--force" in err
    assert sorted(p.name for p in tmp_path.iterdir()) == ["other.txt"]


def test_force_regenerates_and_drops_stale_invoices(tmp_path: Path) -> None:
    assert main(["--seed", "3", "--out", str(tmp_path)]) == 0
    stale = tmp_path / "invoices" / "INV-SEED-999.pdf"
    stale.write_bytes(b"stale")
    (tmp_path / "README.txt").write_text("unrelated")
    assert main(["--seed", "3", "--out", str(tmp_path), "--force"]) == 0
    assert not stale.exists()
    assert (tmp_path / "README.txt").read_text() == "unrelated"
    assert (tmp_path / "master.json").is_file()


def test_default_seed_is_used_when_omitted(tmp_path: Path) -> None:
    import json

    assert main(["--out", str(tmp_path)]) == 0
    assert json.loads((tmp_path / "index.json").read_text())["seed"] == 20261110
