import random

import pytest

from synth.iban import (
    FAKE_BANK_CODES,
    IBAN_LENGTHS,
    KNOWN_TEST_IBANS,
    bank_code,
    is_valid_mod97,
    make_iban,
)


def test_known_real_iban_passes_mod97() -> None:
    # The checker itself is right: a published example IBAN is valid.
    assert is_valid_mod97("DE89370400440532013000")


@pytest.mark.parametrize(
    "iban", ["DE89370400440532013001", "XX", "DE89 3704", "de89370400440532013000"]
)
def test_invalid_ibans_are_rejected(iban: str) -> None:
    assert not is_valid_mod97(iban)


@pytest.mark.parametrize("country", sorted(IBAN_LENGTHS))
def test_generated_iban_is_valid_and_uses_a_fake_bank_code(country: str) -> None:
    rng = random.Random(1)
    for _ in range(50):
        iban = make_iban(country, rng)
        assert iban.startswith(country)
        assert len(iban) == IBAN_LENGTHS[country]
        assert is_valid_mod97(iban)
        assert bank_code(iban) in FAKE_BANK_CODES
        assert iban not in KNOWN_TEST_IBANS


def test_make_iban_rejects_unknown_country() -> None:
    with pytest.raises(ValueError, match="country"):
        make_iban("GB", random.Random(1))


def test_make_iban_is_deterministic() -> None:
    assert make_iban("IT", random.Random(7)) == make_iban("IT", random.Random(7))
