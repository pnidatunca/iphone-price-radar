"""Normalisation shared by the source adapters: canonical model names, storage sizes and colour names.

Sites spell models differently ("iPhone 12 Mini", "iPhone 16E", "iphone se (2nd generation)", "iphone-se-2020",
"Yenilenmiş Apple iPhone 13 Pro Max"). Scope decisions (``Settings.scope.includes``) and joins across sources need
one spelling, so every adapter maps its raw value to the canonical name before anything else looks at it. A value
that cannot be mapped returns None and the caller reports it, instead of guessing.

Marketplace titles carry model, storage and colour inside free text ("Yenilenmiş iPhone 13 128 GB Yıldız Işığı Cep
Telefonu (12 Ay Garantili) - A Kalite", "iPhone 13 Pembe 128GB MLPH3TU/A"); ``model_in_text``, ``storage_in_text``
and ``color_in_text`` find them there. Colours get one Turkish spelling ("Naturel Titanyum", "Sari" and "Natural
Titanium" become "Natürel Titanyum" and "Sarı"); a colour name that is not in ``COLOURS`` is not guessed.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

from price_radar.ingest.schemas import STORAGE_VALUES_GB

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
STORAGE_IN_TEXT = re.compile(r"(?<![\d.,])\d+(?:[.,]\d+)? ?(?:gb|tb)\b")
ASCII_DIGITS = re.compile(r"[0-9]+")
MODEL_WORDS = 4  # "iPhone" is followed by at most four model words: "SE 2. Nesil", "13 Pro Max"
TURKISH_ASCII = str.maketrans("ıİşŞğĞüÜöÖçÇ", "iIsSgGuUoOcC")
# Colour names as Apple and Turkish sellers write them, in their canonical Turkish spelling. Matching ignores case
# and diacritics, so "Naturel Titanyum" and "NATÜREL TİTANYUM" are the same colour.
COLOURS = (
    "Altın",
    "Beyaz",
    "Gri",
    "Gümüş",
    "Kırmızı",
    "Lacivert",
    "Mavi",
    "Mor",
    "Pembe",
    "Sarı",
    "Siyah",
    "Turuncu",
    "Yeşil",
    "Açık Pembe",
    "Ada Çayı",
    "Alp Yeşili",
    "Beyaz Titanyum",
    "Çöl Titanyum",
    "Deniz Mavisi",
    "Derin Mor",
    "Gece Yarısı",
    "Gece Yeşili",
    "Grafit",
    "Köknar Yeşili",
    "Kozmik Turuncu",
    "Laciverttaş",
    "Lavanta",
    "Mavi Titanyum",
    "Mercan",
    "Natürel Titanyum",
    "Pamuk Beyazı",
    "Pasifik Mavisi",
    "Sierra Mavisi",
    "Siyah Titanyum",
    "Sis Mavisi",
    "Uçuk Altın",
    "Ultramarin",
    "Uzay Grisi",
    "Uzay Siyahı",
    "Yıldız Işığı",
)
# English names some titles use, where Apple's Turkish name is certain.
COLOUR_ALIASES = {
    "Black": "Siyah",
    "White": "Beyaz",
    "Silver": "Gümüş",
    "Gold": "Altın",
    "Blue": "Mavi",
    "Green": "Yeşil",
    "Red": "Kırmızı",
    "Purple": "Mor",
    "Pink": "Pembe",
    "Yellow": "Sarı",
    "Graphite": "Grafit",
    "Midnight": "Gece Yarısı",
    "Starlight": "Yıldız Işığı",
    "Space Gray": "Uzay Grisi",
    "Space Grey": "Uzay Grisi",
    "Space Black": "Uzay Siyahı",
    "Deep Purple": "Derin Mor",
    "Sierra Blue": "Sierra Mavisi",
    "Pacific Blue": "Pasifik Mavisi",
    "Alpine Green": "Alp Yeşili",
    "Midnight Green": "Gece Yeşili",
    "Natural Titanium": "Natürel Titanyum",
    "Black Titanium": "Siyah Titanyum",
    "White Titanium": "Beyaz Titanyum",
    "Blue Titanium": "Mavi Titanyum",
    "Desert Titanium": "Çöl Titanyum",
    "Cosmic Orange": "Kozmik Turuncu",
}


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


def model_in_text(text: str | None) -> str | None:
    """The canonical model a free text names, e.g. "Yenilenmiş iPhone 13 Pro 128 GB Grafit" -> "iPhone 13 Pro".

    The longest run of words after "iPhone" that ``canonical_model`` accepts wins, so "iPhone 13 Pro Max 256 GB" is
    the Pro Max and "iPhone 11 64 GB" is the iPhone 11. Text without "iPhone" gives None.
    """
    if not text:
        return None
    words = re.sub(r"[()\[\],/_-]", " ", text.casefold()).replace(".", " ").split()
    for index, word in enumerate(words):
        if not word.startswith("iphone"):
            continue
        rest = ([word.removeprefix("iphone")] if word != "iphone" else []) + words[index + 1 :]
        for size in range(min(MODEL_WORDS, len(rest)), 0, -1):
            if model := canonical_model("iphone " + " ".join(rest[:size])):
                return model
    return None


def storage_in_text(text: str | None) -> int | None:
    """The first iPhone storage size in a free text ("... 128GB ..." -> 128); RAM sizes such as 4 GB are skipped."""
    for match in STORAGE_IN_TEXT.finditer(text.casefold() if text else ""):
        size = storage_gb(match.group(0))
        if size in STORAGE_VALUES_GB:
            return size
    return None


def fold(text: str) -> str:
    """Lower case without diacritics and with single spaces, for matching Turkish text: "Yıldız Işığı" -> "yildiz
    isigi"."""
    plain = unicodedata.normalize("NFKD", text.translate(TURKISH_ASCII))
    return " ".join(plain.encode("ascii", "ignore").decode("ascii").casefold().split())


_COLOUR_NAMES = {fold(name): name for name in COLOURS} | {fold(alias): name for alias, name in COLOUR_ALIASES.items()}
_COLOUR_IN_TEXT = re.compile(
    r"\b(" + "|".join(re.escape(key) for key in sorted(_COLOUR_NAMES, key=len, reverse=True)) + r")\b"
)


def canonical_color(raw: str | None) -> str | None:
    """The canonical spelling of a known colour name ("MAVİ" -> "Mavi", "Natural Titanium" -> "Natürel Titanyum");
    None for anything else."""
    return _COLOUR_NAMES.get(fold(raw)) if raw else None


def color_in_text(text: str | None) -> str | None:
    """The first known colour name in a free text, longest name first ("... Mavi Titanyum ..." is not "Mavi")."""
    match = _COLOUR_IN_TEXT.search(fold(text)) if text else None
    return _COLOUR_NAMES[match.group(1)] if match else None


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
