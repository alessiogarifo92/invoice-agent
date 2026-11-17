"""IBANs: compact form, ISO 7064 mod-97 check, and a regex sweep over free text.

Only the mod-97 checksum is validated, never national BBAN layouts: the synthetic IBANs use
made-up bank codes that no national layout would accept.
"""

import re
import string

_ALLOWED = frozenset(string.ascii_uppercase + string.digits)
# Compact ("ES25ZZZZ72212691...") or printed in groups of four ("FR48 QQZX 8087 ...") on one
# line. Matches with a bad checksum are dropped, so a stray code that looks like one is ignored.
_IBAN = re.compile(
    r"\b[A-Z]{2}\d{2}(?:[A-Z0-9]{11,30}|(?:[ \t]?[A-Z0-9]{4}){2,7}(?:[ \t]?[A-Z0-9]{1,4})?)\b"
)


def compact_iban(value: str) -> str:
    return "".join(value.split()).upper()


def is_valid_mod97(iban: str) -> bool:
    """Structure plus the mod-97-10 checksum, on the compact uppercase form."""
    if not 15 <= len(iban) <= 34:
        return False
    if not (iban[:2].isascii() and iban[:2].isalpha() and iban[:2].isupper()):
        return False
    if not iban[2:4].isdigit() or any(ch not in _ALLOWED for ch in iban):
        return False
    digits = "".join(str(int(ch, 36)) for ch in iban[4:] + iban[:4])
    return int(digits) % 97 == 1


def find_ibans(text: str) -> list[str]:
    """Every distinct mod-97-valid IBAN in `text`, compact, in order of first appearance."""
    found: list[str] = []
    for match in _IBAN.finditer(text):
        iban = compact_iban(match.group(0))
        if is_valid_mod97(iban) and iban not in found:
            found.append(iban)
    return found
