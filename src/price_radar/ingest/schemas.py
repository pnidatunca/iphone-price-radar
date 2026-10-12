"""The raw offer record every source adapter produces (plan section 7.4, updated by the Phase 0 findings).

Phase 0 changed the plan's field list: no site has a per-offer battery health and the warranty is constant, so both
are optional; the offer key is ``getmobil:{variant_id}:{vendor_id}`` or ``easycep:{id}``; and the record carries the
numeric seller id, the stock quantity, the SIM type, the SKU and its condition flags.

Privacy (K12): the record has no field for a seller's display name, slug or store link, and the validator rejects a
payload that carries one, so an adapter cannot store them by accident. Sellers are identified by their numeric id;
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

Platform = Literal["getmobil", "easycep"]
SimType = Literal["physical", "esim"]

PLATFORM_HOSTS = {"getmobil": "getmobil.com", "easycep": "easycep.com"}
STORAGE_VALUES_GB = frozenset({16, 32, 64, 128, 256, 512, 1024, 2048})
MAX_PRICE_TL = Decimal("1000000")  # sanity bound: a larger value is a parsing error (e.g. kuruş read as lira)
CENTS = Decimal("0.01")
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
FORBIDDEN_KEY = re.compile(
    r"name|slug|(vendor|seller|store)_?title|store_?(link|url)|address|phone_?(number|no)|telephone|e_?mail"
    r"|tax_?(number|no|id|office)|kep",
    re.IGNORECASE,
)
FORBIDDEN_VALUE = re.compile(r"/magazalarimiz/", re.IGNORECASE)


class RawOffer(BaseModel):
    """One offer as seen on one day. ``model`` is the canonical name (see ``normalize.canonical_model``).

    ``sim_type`` and the SKU fields are None or empty when the page that carries them was not read in this run
    (Easycep shows them only on unit pages, which are fetched once per id).
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

    @field_validator("price_tl")
    @classmethod
    def _to_cents(cls, value: Decimal) -> Decimal:
        return value.quantize(CENTS)

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
        expected = payload_hash(self)
        if self.payload_hash and self.payload_hash != expected:
            raise ValueError("payload_hash does not match the offer fields")
        # The model is frozen; the hash is derived once, here, so it can never disagree with the fields.
        object.__setattr__(self, "payload_hash", expected)
        return self


def error_summary(err: Exception) -> str:
    """One line per failed field, short enough for the run summary."""
    if isinstance(err, ValidationError):
        return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'offer'}: {e['msg']}" for e in err.errors())
    return str(err)


def payload_hash(offer: RawOffer) -> str:
    """SHA-256 over the offer fields that matter for change detection (``HASHED_FIELDS``)."""
    stable = {name: getattr(offer, name) for name in HASHED_FIELDS}
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
