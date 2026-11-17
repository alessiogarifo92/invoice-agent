"""Synthetic IBANs: valid mod-97 check digits, made-up bank codes.

The BBAN is the made-up 4-letter bank code followed by digits, padded to the country's real
IBAN length. National BBAN layouts are deliberately not followed (IT, DE, FR and ES use
numeric bank codes), so no generated IBAN can belong to a real bank account.
"""

import random
import string

# Bank codes no real bank uses. Every IBAN the generator writes starts its BBAN with one.
FAKE_BANK_CODES: tuple[str, ...] = ("ZZZZ", "XQXQ", "QXZQ", "ZQZX", "XZXZ", "QQZX")

# Real IBAN lengths per country (ISO 13616 registry).
IBAN_LENGTHS: dict[str, int] = {"IT": 27, "DE": 22, "FR": 27, "ES": 24, "NL": 18}

# Widely published example IBANs (bank docs, Wikipedia, test suites). Never generate these.
KNOWN_TEST_IBANS: frozenset[str] = frozenset(
    {
        "DE89370400440532013000",
        "GB82WEST12345698765432",
        "GB29NWBK60161331926819",
        "NL91ABNA0417164300",
        "NL02ABNA0123456789",
        "FR1420041010050500013M02606",
        "FR7630006000011234567890189",
        "IT60X0542811101000000123456",
        "ES9121000418450200051332",
        "ES7921000813610123456789",
        "BE68539007547034",
        "CH9300762011623852957",
        "AT611904300234573201",
    }
)

_BANK_CODE_LEN = 4


def _to_digits(text: str) -> str:
    return "".join(str(int(ch, 36)) for ch in text)


def is_valid_mod97(iban: str) -> bool:
    """Structural check plus the ISO 7064 mod-97-10 checksum (compact, uppercase form)."""
    if len(iban) < 15 or len(iban) > 34:
        return False
    if not (iban[:2].isalpha() and iban[:2].isupper() and iban[2:4].isdigit()):
        return False
    allowed = set(string.ascii_uppercase + string.digits)
    if any(ch not in allowed for ch in iban):
        return False
    return int(_to_digits(iban[4:] + iban[:4])) % 97 == 1


def bank_code(iban: str) -> str:
    """The made-up bank code segment of an IBAN produced by `make_iban`."""
    return iban[4 : 4 + _BANK_CODE_LEN]


def make_iban(country: str, rng: random.Random) -> str:
    """A mod-97-valid IBAN for `country` whose bank code is one of FAKE_BANK_CODES."""
    if country not in IBAN_LENGTHS:
        raise ValueError(f"unsupported IBAN country {country!r}")
    digits_len = IBAN_LENGTHS[country] - 4 - _BANK_CODE_LEN
    while True:
        bban = rng.choice(FAKE_BANK_CODES) + "".join(
            rng.choice(string.digits) for _ in range(digits_len)
        )
        check = 98 - int(_to_digits(bban + country + "00")) % 97
        iban = f"{country}{check:02d}{bban}"
        if iban not in KNOWN_TEST_IBANS:
            return iban
