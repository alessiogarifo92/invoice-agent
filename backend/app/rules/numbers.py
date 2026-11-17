"""Number helpers shared by the rules and the matching layer: money, invoice numbers, trigrams."""

import re
import struct
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")

_GROUPS = re.compile(r"[A-Za-z]+|\d+")
# pg_trgm treats every non-alphanumeric character (underscore included) as a word separator.
_TRGM_WORD = re.compile(r"[^\W_]+")


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def fmt_money(value: Decimal) -> str:
    return str(money(value))


def normalize_invoice_number(number: str) -> str:
    """Comparison key: uppercase alphanumeric groups, numeric groups without leading zeros.

    Same logic as synth's `normalize_invoice_number` (copied, not imported: the backend never
    depends on the generator). "RE-00356" and the resent "RE 356" both become "RE356".
    """
    return "".join(str(int(g)) if g.isdigit() else g.upper() for g in _GROUPS.findall(number))


def _trigrams(text: str) -> set[str]:
    grams: set[str] = set()
    for word in _TRGM_WORD.findall(text.lower()):
        padded = f"  {word} "
        grams.update(padded[i : i + 3] for i in range(len(padded) - 2))
    return grams


def _float4(value: float) -> float:
    """Round to single precision, the `real` type pg_trgm's similarity() returns."""
    result: float = struct.unpack("f", struct.pack("f", value))[0]
    return result


def trigram_similarity(a: str, b: str) -> float:
    """pg_trgm's similarity(): shared trigrams over all distinct trigrams of both strings.

    The in-memory matcher uses it so its results agree with the SQL matcher. Words are
    lowercased and padded with two spaces in front and one behind, as pg_trgm does.
    """
    left, right = _trigrams(a), _trigrams(b)
    union = left | right
    if not union:
        return 0.0
    return _float4(len(left & right) / len(union))


def at_least(similarity: float, threshold: float) -> bool:
    """`similarity >= threshold` compared in single precision, like `real >= real` in SQL."""
    return _float4(similarity) >= _float4(threshold)


def parse_amount(raw: str) -> Decimal | None:
    """An amount as printed ("1,736.01", "1.736,01", "1'736.01") or None if it is not one.

    With both separators the last one is the decimal mark. A lone comma followed by exactly
    two digits is a decimal comma; otherwise commas and repeated dots group thousands.
    """
    text = raw.strip().replace("'", "").replace(" ", "")
    if not text or not re.fullmatch(r"\d[\d.,]*", text):
        return None
    if "," in text and "." in text:
        decimal_mark = "," if text.rfind(",") > text.rfind(".") else "."
        grouping = "." if decimal_mark == "," else ","
        if text.count(decimal_mark) != 1:
            return None
        text = text.replace(grouping, "").replace(decimal_mark, ".")
    elif "," in text:
        if re.fullmatch(r"\d+,\d{2}", text):
            text = text.replace(",", ".")
        elif re.fullmatch(r"\d{1,3}(?:,\d{3})+", text):
            text = text.replace(",", "")
        else:
            return None
    elif text.count(".") > 1:
        if not re.fullmatch(r"\d{1,3}(?:\.\d{3})+", text):
            return None
        text = text.replace(".", "")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None
