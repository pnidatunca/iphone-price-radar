"""Normalisation shared by the source adapters: canonical model names and storage sizes.

Sites spell models differently ("iPhone 12 Mini", "iPhone 16E", "iphone se (2nd generation)", "iphone-se-2020",
"Yenilenmiş Apple iPhone 13 Pro Max"). Scope decisions (``Settings.scope.includes``) and joins across sources need
one spelling, so every adapter maps its raw value to the canonical name before anything else looks at it. A value
that cannot be mapped returns None and the caller reports it, instead of guessing.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

SUFFIXES = {"pro max": "Pro Max", "pro": "Pro", "plus": "Plus", "mini": "mini"}
SE_GENERATIONS = {
    "2016": 2016,
    "1st generation": 2016,
    "1 nesil": 2016,
    "2020": 2020,
    "2nd generation": 2020,
    "2 nesil": 2020,
    "2022": 2022,
    "3rd generation": 2022,
    "3 nesil": 2022,
}
NUMBERED = re.compile(r"(?P<gen>\d{1,2})(?P<letter>[sce])?(?: (?P<suffix>pro max|pro|plus|mini))?")
X_FAMILY = re.compile(r"x(?P<kind>[sr])?(?: (?P<max>max))?")
STORAGE = re.compile(r"(?P<size>\d+(?:[.,]\d+)?) ?(?P<unit>gb|tb)")
ASCII_DIGITS = re.compile(r"[0-9]+")


def canonical_model(raw: str | None) -> str | None:
    """Map a site's model spelling to the canonical name, e.g. "iPhone 13 Pro Max" or "iPhone SE (2022)"."""
    if not raw:
        return None
    text = raw.casefold().replace("-", " ").replace("_", " ").replace(".", " ")
    text = re.sub(r"[()]", " ", text)
    text = " ".join(text.split())
    marker = re.search(r"\biphone ?", text)
    if marker is None:
        return None
    rest = text[marker.end() :]
    if rest == "air":
        return "iPhone Air"
    if rest == "se" or rest.startswith("se "):
        year = SE_GENERATIONS.get(rest[3:])
        return f"iPhone SE ({year})" if year else None
    if match := X_FAMILY.fullmatch(rest):
        if match["kind"] == "r" and match["max"]:
            return None
        name = "iPhone X" + (match["kind"] or "").upper()
        return name + (" Max" if match["max"] else "")
    if match := NUMBERED.fullmatch(rest):
        name = f"iPhone {int(match['gen'])}{match['letter'] or ''}"
        return f"{name} {SUFFIXES[match['suffix']]}" if match["suffix"] else name
    return None


def storage_gb(raw: str | None) -> int | None:
    """Storage size in GB ("128 GB" -> 128, "1 TB" -> 1024); None when the value is not a storage size."""
    if not raw:
        return None
    match = STORAGE.fullmatch(" ".join(raw.casefold().split()))
    if match is None:
        return None
    size = float(match["size"].replace(",", "."))
    gigabytes = size * 1024 if match["unit"] == "tb" else size
    return int(gigabytes) if gigabytes.is_integer() else None


def as_int(value: Any) -> int | None:
    """An int from page data: ints and digit strings. Booleans (an int subclass) and anything else give None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and ASCII_DIGITS.fullmatch(value.strip()):
        return int(value)
    return None


def as_decimal(value: Any) -> Decimal | None:
    """A price from page data, which has numbers (31999, 36749.15) and strings ("106999.00")."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def clean_text(value: Any) -> str:
    """A string with whitespace collapsed; anything that is not a string gives ""."""
    return " ".join(value.split()) if isinstance(value, str) else ""
