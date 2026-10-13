"""The raw offer record every source adapter produces (plan section 7.4, updated by the Phase 0 findings).

Phase 0 changed the plan's field list: no site has a per-offer battery health and the warranty is constant, so both
are optional; the offer key is ``getmobil:{variant_id}:{vendor_id}``, ``easycep:{id}`` or
``trendyol:{content_id}:{merchant_id}``; and the record carries the numeric seller id, the stock quantity, the SIM
type, the SKU and its condition flags.

Prices mean the same on every platform:

* ``price_tl`` is the price a buyer pays today for that offer (Trendyol: after the automatic basket promotion;
  per-user coupons are ignored everywhere);
* ``list_price_tl`` is the price before any temporary discount that the page itself shows for that offer: Trendyol's
  ``sellingPrice``, Getmobil's struck-through price of the buybox offer (``priceInfo.strikethrough``; other sellers show
  none), Easycep's ``basePrice``. When the page shows no separate earlier price, ``list_price_tl`` equals ``price_tl``;
* ``promo_pct`` is the discount between the two, in percent of ``list_price_tl``, computed from them (0 without one).

``list_price_and_promo`` applies the rule for the adapters, which always fill both fields. A shown earlier price below
the price paid, or one that would mean a discount above ``MAX_PLAUSIBLE_PROMO`` percent, is not stored: the offer gets
``list_price_tl = price_tl`` and ``promo_pct = 0`` and the adapter counts a warning. The two fields are optional in the
record only so that offers stored before they existed still validate.

Privacy (K12, K22): the record has no field for a seller's display name, slug, store link or legal details (legal
name, tax number, registration number, e-mail, address), and the validator rejects a payload that carries one, so an
adapter cannot store them by accident. Sellers are identified by their numeric id;
``offer_url`` is the link that opens the offer of exactly that seller, so a reader can still go and buy it.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

Platform = Literal["getmobil", "easycep", "trendyol"]
SimType = Literal["physical", "esim"]

PLATFORM_HOSTS = {"getmobil": "getmobil.com", "easycep": "easycep.com", "trendyol": "www.trendyol.com"}
STORAGE_VALUES_GB = frozenset({16, 32, 64, 128, 256, 512, 1024, 2048})
MAX_PRICE_TL = Decimal("1000000")  # sanity bound: a larger value is a parsing error (e.g. kuruş read as lira)
CENTS = Decimal("0.01")
PROMO_TOLERANCE = Decimal("0.01")  # promo_pct is rounded to two decimals
MAX_PLAUSIBLE_PROMO = Decimal("50")  # a larger discount on an iPhone means the shown earlier price is wrong
NO_PROMO = Decimal("0.00")
# Fields whose change means the offer itself changed. Volatile payload details (seller score, delivery days,
# popularity counters) stay out of the hash, so "store the payload only when the hash changes" (plan 7.4) still saves
# space.
HASHED_FIELDS = (
    "offer_key",
    "model",
    "storage_gb",
    "color",
    "condition_raw",
    "sim_type",
    "battery_raw",
    "warranty_raw",
    "sku",
    "condition_flags",
    "seller_id",
    "is_buybox",
    "price_tl",
    "stock_qty",
    "in_stock",
)
# Added after offers were first stored: list_price_tl enters the hash only when it differs from price_tl (a shown
# discount), so the hash of an offer without one is the same as before the field existed. promo_pct is left out
# because it follows from price_tl and list_price_tl.
OPTIONAL_HASHED_FIELDS = ("list_price_tl",)
FORBIDDEN_KEY = re.compile(
    r"name|slug|(vendor|seller|store)_?title|store_?(link|url)|address|phone_?(number|no)|telephone|e_?mail"
    r"|tax_?(number|no|id|office)|registration_?(number|no)|mersis|kep",
    re.IGNORECASE,
)
FORBIDDEN_VALUE = re.compile(r"/magazalarimiz/|/magaza/", re.IGNORECASE)  # Getmobil and Trendyol store links


class RawOffer(BaseModel):
    """One offer as seen on one day. ``model`` is the canonical name (see ``normalize.canonical_model``).

    ``sim_type`` and the SKU fields are None or empty when the page that carries them was not read in this run
    (Easycep shows them only on unit pages, which are fetched once per id) or the site does not show them (Trendyol).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: Platform
    offer_key: str
    snapshot_date: date
    fetched_at: AwareDatetime
    model: str
    storage_gb: int
    color: str
    condition_raw: str
    sim_type: SimType | None
    battery_raw: str | None = None
    warranty_raw: str | None = None
    sku: str | None = None
    condition_flags: list[str] = Field(default_factory=list)
    seller_id: int | None = Field(default=None, gt=0)
    is_buybox: bool
    price_tl: Decimal = Field(gt=0, le=MAX_PRICE_TL)
    list_price_tl: Decimal | None = Field(default=None, gt=0, le=MAX_PRICE_TL)
    promo_pct: Decimal | None = Field(default=None, ge=0, lt=100)
    stock_qty: int | None = Field(default=None, ge=0)
    in_stock: bool
    product_url: str
    offer_url: str
    payload: dict[str, Any]
    payload_hash: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")
    parser_version: str = Field(min_length=1)

    @field_validator("model", "color", "condition_raw")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("storage_gb")
    @classmethod
    def _known_storage(cls, value: int) -> int:
        if value not in STORAGE_VALUES_GB:
            raise ValueError(f"{value} GB is not an iPhone storage size")
        return value

    @field_validator("price_tl", "list_price_tl", "promo_pct")
    @classmethod
    def _to_cents(cls, value: Decimal | None) -> Decimal | None:
        return None if value is None else value.quantize(CENTS)

    @field_validator("payload")
    @classmethod
    def _no_personal_data(cls, value: dict[str, Any]) -> dict[str, Any]:
        if problem := personal_data_path(value):
            raise ValueError(f"payload must not contain seller names, slugs or store links ({problem})")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> RawOffer:
        if not self.offer_key.startswith(f"{self.platform}:"):
            raise ValueError(f"offer_key {self.offer_key!r} does not belong to {self.platform}")
        for field in ("product_url", "offer_url"):
            parts = urlsplit(getattr(self, field))
            if parts.scheme != "https" or parts.hostname != PLATFORM_HOSTS[self.platform]:
                raise ValueError(f"{field} must be an https URL on {PLATFORM_HOSTS[self.platform]}")
        self._check_list_price()
        expected = payload_hash(self)
        if self.payload_hash and self.payload_hash != expected:
            raise ValueError("payload_hash does not match the offer fields")
        # The model is frozen; the hash is derived once, here, so it can never disagree with the fields.
        object.__setattr__(self, "payload_hash", expected)
        return self

    def _check_list_price(self) -> None:
        """The price paid is at most the sticker price, and the promotion percent agrees with the two."""
        if self.list_price_tl is None:
            if self.promo_pct is not None:
                raise ValueError("promo_pct needs list_price_tl")
            return
        if self.price_tl > self.list_price_tl:
            raise ValueError("price_tl must not exceed list_price_tl")
        if self.promo_pct is not None:
            expected = (self.list_price_tl - self.price_tl) / self.list_price_tl * 100
            if abs(expected - self.promo_pct) > PROMO_TOLERANCE:
                raise ValueError(f"promo_pct {self.promo_pct} does not match the prices ({expected:.2f})")


def list_price_and_promo(paid: Decimal, shown: Decimal | None) -> tuple[Decimal, Decimal, str | None]:
    """``(list_price_tl, promo_pct, problem)`` for an offer whose page shows ``shown`` as its price before a temporary
    discount, or None when it shows none. An implausible ``shown`` (below the price paid, or a discount above
    ``MAX_PLAUSIBLE_PROMO`` percent) is not used: the result is ``(paid, 0)`` with the reason, for the adapter to warn.
    A price paid that is not positive gets no discount either; RawOffer rejects the offer.
    """
    if shown is None or shown == paid or paid <= 0:
        return paid, NO_PROMO, None
    if not shown.is_finite() or shown < paid:
        return paid, NO_PROMO, f"list price {shown} is below the price paid {paid}"
    promo = ((shown - paid) / shown * 100).quantize(CENTS)
    if promo > MAX_PLAUSIBLE_PROMO or shown > MAX_PRICE_TL:
        return paid, NO_PROMO, f"list price {shown} would mean a {promo}% discount on {paid}"
    return shown, promo, None


def error_summary(err: Exception) -> str:
    """One line per failed field, short enough for the run summary."""
    if isinstance(err, ValidationError):
        return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'offer'}: {e['msg']}" for e in err.errors())
    return str(err)


def payload_hash(offer: RawOffer) -> str:
    """SHA-256 over the offer fields that matter for change detection: ``HASHED_FIELDS``, and ``list_price_tl`` when it
    differs from ``price_tl``."""
    stable = {name: getattr(offer, name) for name in HASHED_FIELDS}
    stable |= {
        name: getattr(offer, name)
        for name in OPTIONAL_HASHED_FIELDS
        if getattr(offer, name) is not None and getattr(offer, name) != offer.price_tl
    }
    text = json.dumps(stable, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def personal_data_path(node: Any, path: str = "payload") -> str | None:
    """Path of the first key or value that looks like a seller name, slug or store link; None if there is none."""
    if isinstance(node, dict):
        for key, item in node.items():
            if FORBIDDEN_KEY.search(str(key)):
                return f"{path}.{key}"
            if found := personal_data_path(item, f"{path}.{key}"):
                return found
    elif isinstance(node, list | tuple):
        for index, item in enumerate(node):
            if found := personal_data_path(item, f"{path}[{index}]"):
                return found
    elif isinstance(node, str) and FORBIDDEN_VALUE.search(node):
        return path
    return None
