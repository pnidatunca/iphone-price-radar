"""Easycep adapter: offers from the refurbished-Apple listing, SKU details from the unit pages of new ids.

Easycep sells its own stock, so there is one seller and no seller id. A listing item is one SKU slot (model x storage
x colour x grade x condition flags) holding one or two units, and ``/kategori/apple-2`` lists every in-stock
refurbished iPhone with price, quantity, grade and battery range. The daily offers therefore come from its pages
(``?page=1..pageCount``). The listing's default order can shift between page requests, so every page must report the
same total and the distinct ids must add up to it; otherwise the listing is read once more.

A unit page adds what the listing lacks: the SKU, whose suffix tokens are condition flags that move the price
(PU, EU, KU, 2ESIM; their meaning is not published), the EAN and the warranty. They do not change for an id, so a unit
page is read only for ids the caller has not seen before (``CrawlContext.known_ids``). Unit pages of sold units stay
online with the last price, so they are never a source of offers.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from pydantic import ValidationError

from price_radar.ingest import urls
from price_radar.ingest.crawl import CrawlContext
from price_radar.ingest.http_client import CrawlStopped
from price_radar.ingest.normalize import as_decimal, as_int, canonical_model, clean_text, storage_gb
from price_radar.ingest.rsc import Flight, FlightError
from price_radar.ingest.schemas import RawOffer, error_summary

log = logging.getLogger(__name__)

SOURCE = "easycep"
PARSER_VERSION = "easycep-1"
HOST = "easycep.com"
BASE_URL = f"https://{HOST}"
LISTING_PATH = "/kategori/apple-2"
URL_FORMS = (
    urls.UrlForm("listing", re.compile(re.escape(LISTING_PATH)), ("page",)),
    urls.UrlForm("unit", re.compile(rf"/{urls.SLUG}-cep-telefonu-\d+")),
)
SKU = re.compile(r"(?P<prefix>MS?)-PIP(?P<body>.+)-(?P<grade>[A-Z])")
SKU_STORAGE = re.compile(r"\d+(?:GB|TB)")
KNOWN_FLAGS = ("2ESIM", "PU", "EU", "KU")
GRADES = {"M": "Mükemmel", "C": "Çok İyi", "I": "İyi", "O": "Outlet"}
KNOWN_ATTRIBUTES = frozenset({"Hafıza", "Renk", "Kozmetik", "Pil Sağlığı"})
REFURBISHED_PREFIX = "Yenilenmiş"
DIGITS = re.compile(r"[0-9]+")


class ParseError(ValueError):
    """The page does not have the structure this parser expects."""


@dataclass(frozen=True)
class ListingPage:
    total: int
    page: int
    page_count: int
    items: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class SkuInfo:
    """A decoded SKU such as ``M-PIP13128GBZZEU-M``: prefix (M refurbished, MS new), grade letter and flags.

    The SKU reads model, storage, colour code, condition flags and grade (M, C, I, O). ``flags`` lists the known
    flags in SKU order, followed by any token this parser does not know (for example the digit in ``...ZZ3-I``),
    kept as raw text so that it is not lost.
    """

    raw: str
    prefix: str | None
    grade: str | None
    flags: tuple[str, ...]


@dataclass(frozen=True)
class UnitInfo:
    unit_id: str
    sku: SkuInfo | None
    ean: str | None
    warranty: tuple[str, ...]
    is_new: bool
    in_stock: bool
    quantity: int | None


def check_url(url: str) -> str:
    """Name of the allowed form ``url`` has ("listing" or "unit"); raises ``urls.UrlNotAllowed``."""
    return urls.check(url, HOST, URL_FORMS)


def listing_url(page: int) -> str:
    url = f"{BASE_URL}{LISTING_PATH}?page={page}"
    check_url(url)
    return url


def unit_url(path: str) -> str:
    """Absolute unit page URL from the relative ``url`` of a listing item; anything but a unit page is refused."""
    url = f"{BASE_URL}/{path.lstrip('/')}"
    if check_url(url) != "unit":
        raise urls.UrlNotAllowed(f"{url}: not a unit page")
    return url


def parse_listing_page(html: str) -> ListingPage:
    flight = _flight(html)
    listing = next(
        (d["list"] for d in flight.find_dicts("list") if isinstance(d["list"], dict) and "pageCount" in d["list"]),
        None,
    )
    if listing is None:
        raise ParseError("no product list (products.body.list) in the page")
    total, page, page_count = (as_int(listing.get(key)) for key in ("total", "page", "pageCount"))
    items = listing.get("data")
    if total is None or page is None or page_count is None or not isinstance(items, list):
        raise ParseError("product list without total, page, pageCount or data")
    return ListingPage(total, page, page_count, tuple(item for item in items if isinstance(item, dict)))


def parse_unit_page(html: str) -> UnitInfo:
    flight = _flight(html)
    product = next(
        (
            d["product"]
            for d in flight.find_dicts("product")
            if isinstance(d["product"], dict) and isinstance(d["product"].get("detail"), dict)
        ),
        None,
    )
    if product is None:
        raise ParseError("no product detail in the page")
    detail = product["detail"]
    unit_id = str(detail.get("id") or "")
    if not DIGITS.fullmatch(unit_id):
        raise ParseError(f"unit page without a numeric id: {detail.get('id')!r}")
    raw_sku = detail.get("sku")
    sku = parse_sku(raw_sku) if isinstance(raw_sku, str) and raw_sku.strip() else None
    quantity = as_int(detail.get("quantity"))
    guarantee = product.get("additionalGuaranteeData")
    return UnitInfo(
        unit_id=unit_id,
        sku=sku,
        ean=normalize_ean(detail.get("ean")),
        warranty=tuple(x for x in guarantee if isinstance(x, str)) if isinstance(guarantee, list) else (),
        is_new=detail.get("isNew") in (1, "1", True) or (sku is not None and sku.prefix == "MS"),
        in_stock=detail.get("stockStatus") is True and (quantity or 0) > 0,
        quantity=quantity,
    )


def parse_sku(sku: str) -> SkuInfo:
    """Decode an Easycep SKU. Flags are read from the end of the part after the storage token."""
    raw = sku.strip()
    match = SKU.fullmatch(raw)
    if match is None:
        return SkuInfo(raw, None, None, ())
    body = match["body"]
    storages = list(SKU_STORAGE.finditer(body))
    tail = body[storages[-1].end() :] if storages else body
    flags: list[str] = []
    stripped = True
    while stripped:
        stripped = False
        for flag in KNOWN_FLAGS:
            if tail.endswith(flag) and len(tail) > len(flag):
                flags.insert(0, flag)
                tail = tail[: -len(flag)]
                stripped = True
                break
    unknown = DIGITS.findall(tail)  # colour codes are letters; a digit next to one is a token nobody has explained
    return SkuInfo(raw, match["prefix"], match["grade"], tuple(flags + unknown))


def normalize_ean(raw: Any) -> str | None:
    """EANs come as 12-digit UPC-A or 13 digits with a leading zero; both become the same 13-digit code."""
    if raw is None:
        return None
    digits = str(raw).strip()
    if not DIGITS.fullmatch(digits):
        return None
    if len(digits) == 14 and digits.startswith("0"):
        digits = digits[1:]
    return digits.zfill(13) if 8 <= len(digits) <= 13 else None


def item_model(item: dict[str, Any]) -> str | None:
    category = item.get("category")
    return canonical_model(category.get("name") if isinstance(category, dict) else None)


def attributes(item: dict[str, Any]) -> dict[str, Any]:
    return {
        str(group.get("group_name")): group.get("value")
        for group in item.get("attributeGroup") or []
        if isinstance(group, dict) and group.get("group_name")
    }


def build_offer(item: dict[str, Any], *, unit: UnitInfo | None, fetched_at: datetime, snapshot_date: date) -> RawOffer:
    """The offer of one listing item, completed with the unit page details when ``unit`` is given.

    Raises ValueError (pydantic's ValidationError is one) with the reason when the item cannot be an offer.
    """
    item_id = str(item.get("id") or "")
    if not DIGITS.fullmatch(item_id):
        raise ValueError(f"listing item without a numeric id: {item.get('id')!r}")
    model = item_model(item)
    if model is None:
        raise ValueError(f"unknown model {(item.get('category') or {}).get('name')!r}")
    url = unit_url(str(item.get("url") or ""))
    attrs = attributes(item)
    storage = storage_gb(clean_text(attrs.get("Hafıza")))
    if storage is None:
        raise ValueError(f"unknown storage {attrs.get('Hafıza')!r}")
    price = item.get("price") if isinstance(item.get("price"), dict) else {}
    quantity = as_int(item.get("quantity"))
    flags = list(unit.sku.flags) if unit and unit.sku else []
    category = item.get("category") if isinstance(item.get("category"), dict) else {}
    payload = {
        "id": item_id,
        "title": item.get("productTitle"),
        "model_raw": category.get("name"),
        "category_id": category.get("id"),
        "attributes": attrs,
        "price": {key: price.get(key) for key in ("currency", "basePrice", "salePrice", "discountRate")},
        "status": item.get("status"),
        "stock_status": item.get("stockStatus"),
        "quantity": quantity,
        "badges": [
            b["name"] for b in item.get("badges") or [] if isinstance(b, dict) and isinstance(b.get("name"), str)
        ],
        "unit": _unit_payload(unit),
    }
    return RawOffer(
        platform=SOURCE,
        offer_key=f"easycep:{item_id}",
        snapshot_date=snapshot_date,
        fetched_at=fetched_at,
        model=model,
        storage_gb=storage,
        color=clean_text(attrs.get("Renk")),
        condition_raw=clean_text(attrs.get("Kozmetik")),
        sim_type=_sim_type(unit),
        battery_raw=clean_text(attrs.get("Pil Sağlığı")) or None,
        warranty_raw=_warranty(unit),
        sku=unit.sku.raw if unit and unit.sku else None,
        condition_flags=flags,
        seller_id=None,
        is_buybox=True,
        price_tl=as_decimal(price.get("salePrice")),
        stock_qty=quantity,
        in_stock=item.get("stockStatus") is True and (quantity or 0) > 0,
        product_url=url,
        offer_url=url,
        payload=payload,
        parser_version=PARSER_VERSION,
    )


def crawl(ctx: CrawlContext) -> Iterator[RawOffer]:
    """Read the whole listing, then yield one offer per in-scope item, reading the unit pages of unseen ids.

    When the client stops (budget spent, bot challenge), the offers of the listing pages already read are still
    yielded, without unit details, and the stop is raised afterwards.
    """
    items, stopped = _read_listing(ctx)
    units_read = 0
    unknown_groups: set[str] = set()
    for item_id, (item, seen_at) in items.items():
        model = item_model(item)
        if model is None:
            ctx.stats.fail("item", str(item.get("url")), f"unknown model {(item.get('category') or {}).get('name')!r}")
            continue
        if not ctx.scope.includes(model):
            ctx.stats.counters["items_out_of_scope"] += 1
            continue
        if not str(item.get("productTitle") or "").startswith(REFURBISHED_PREFIX):
            ctx.stats.counters["items_not_refurbished"] += 1
            continue
        for group in sorted(set(attributes(item)) - KNOWN_ATTRIBUTES - unknown_groups):
            unknown_groups.add(group)
            ctx.stats.warn(f"new attribute group {group!r} on the listing")
        unit = None
        if item_id in ctx.known_ids:
            pass
        elif stopped is not None:
            ctx.stats.counters["units_not_read_after_stop"] += 1
        elif ctx.max_variants is not None and units_read >= ctx.max_variants:
            ctx.stats.counters["units_over_limit"] += 1
        else:
            units_read += 1
            try:
                unit = _read_unit(ctx, item_id, str(item.get("url") or ""))
            except CrawlStopped as err:
                stopped = err
        if unit is not None:
            if unit.is_new:
                ctx.stats.counters["items_new_device"] += 1
                continue
            _check_grade(ctx, item_id, unit, attributes(item).get("Kozmetik"))
        try:
            offer = build_offer(item, unit=unit, fetched_at=seen_at, snapshot_date=ctx.snapshot_date)
        except (ValidationError, ValueError) as err:
            ctx.stats.counters["offers_rejected"] += 1
            ctx.stats.warn(f"rejected offer easycep:{item_id}: {error_summary(err)}")
            continue
        ctx.stats.sellers_per_variant[1] += 1
        yield offer
    if stopped is not None:
        raise stopped


@dataclass
class _ListingRead:
    pages: dict[int, tuple[ListingPage, datetime] | None] = field(default_factory=dict)
    stopped: CrawlStopped | None = None

    @property
    def complete(self) -> bool:
        """Every page was read, all report the same total, and the distinct ids add up to it."""
        read = [entry[0] for entry in self.pages.values() if entry is not None]
        if self.stopped is not None or not read or len(read) != len(self.pages):
            return False
        ids = {str(item.get("id")) for page in read for item in page.items}
        return len({page.total for page in read}) == 1 and len(ids) == read[0].total


def _read_listing(ctx: CrawlContext) -> tuple[dict[str, tuple[dict[str, Any], datetime]], CrawlStopped | None]:
    """All listing items by id, in listing order, with the time their page was fetched, plus a pending stop."""
    reading = _read_listing_once(ctx)
    stopped, refetched = reading.stopped, False
    if stopped is None and not reading.complete:
        refetched = True
        log.info("easycep listing is inconsistent, reading it once more")
        again = _read_listing_once(ctx)
        stopped = again.stopped
        if again.stopped is None:  # a second reading cut short is worse than a full first one
            reading = again
    items: dict[str, tuple[dict[str, Any], datetime]] = {}
    for number in sorted(reading.pages):
        entry = reading.pages[number]
        for item in entry[0].items if entry else ():
            items.setdefault(str(item.get("id")), (item, entry[1]))
    totals = sorted({entry[0].total for entry in reading.pages.values() if entry})
    ctx.stats.discovery.update(
        listing_totals=totals,
        listing_pages=len(reading.pages),
        items_seen=len(items),
        refetched=refetched,
        complete=reading.complete,
    )
    ctx.stats.counters["variants_discovered"] += len(items)
    if not reading.complete and stopped is None:
        ctx.stats.warn(f"listing still inconsistent after a re-fetch: totals {totals}, {len(items)} distinct ids")
    elif reading.complete and not items:
        ctx.stats.warn("listing is empty")
    return items, stopped


def _read_listing_once(ctx: CrawlContext) -> _ListingRead:
    reading = _ListingRead()
    try:
        reading.pages[1] = first = _fetch_listing(ctx, 1)
        for number in range(2, first[0].page_count + 1 if first else 0):
            reading.pages[number] = _fetch_listing(ctx, number)
    except CrawlStopped as err:
        reading.stopped = err
    return reading


def _fetch_listing(ctx: CrawlContext, number: int) -> tuple[ListingPage, datetime] | None:
    url = listing_url(number)
    response = ctx.get(url, "listing")
    if response is None:
        return None
    seen_at = ctx.now()
    if response.status_code != 200:
        ctx.stats.fail("listing", url, f"HTTP {response.status_code}")
        return None
    try:
        page = parse_listing_page(response.text)
    except ParseError as err:
        ctx.stats.fail("listing", url, str(err))
        return None
    return page, seen_at


def _read_unit(ctx: CrawlContext, item_id: str, path: str) -> UnitInfo | None:
    try:
        url = unit_url(path)
    except urls.UrlNotAllowed as err:
        ctx.stats.refuse("unit", path, str(err))
        return None
    response = ctx.get(url, "unit")
    if response is None:
        return None
    if response.status_code != 200:
        ctx.stats.fail("unit", url, f"HTTP {response.status_code}")
        return None
    try:
        unit = parse_unit_page(response.text)
    except ParseError as err:
        ctx.stats.fail("unit", url, str(err))
        return None
    if unit.unit_id != item_id:
        ctx.stats.warn(f"unit page of {item_id} shows unit {unit.unit_id}; its details are not used")
        return None
    if not unit.in_stock:
        ctx.stats.warn(f"unit {item_id} is listed but its unit page says it is out of stock")
    return unit


def _check_grade(ctx: CrawlContext, item_id: str, unit: UnitInfo, condition: Any) -> None:
    grade = unit.sku.grade if unit.sku else None
    if grade and condition and GRADES.get(grade) != condition:
        ctx.stats.warn(f"unit {item_id}: SKU grade {grade} does not match the listed condition")


def _sim_type(unit: UnitInfo | None) -> str | None:
    """Only the SKU tells the SIM type: "2ESIM" marks eSIM-only (US) devices. Unknown without the unit page."""
    if unit is None or unit.sku is None:
        return None
    return "esim" if "2ESIM" in unit.sku.flags else "physical"


def _warranty(unit: UnitInfo | None) -> str | None:
    """The warranty items of the unit page's guarantee list, e.g. "12 Ay Garanti"."""
    if unit is None:
        return None
    return "; ".join(item for item in unit.warranty if "garanti" in item.casefold()) or None


def _unit_payload(unit: UnitInfo | None) -> dict[str, Any] | None:
    if unit is None:
        return None
    return {
        "sku": unit.sku.raw if unit.sku else None,
        "sku_prefix": unit.sku.prefix if unit.sku else None,
        "sku_grade": unit.sku.grade if unit.sku else None,
        "flags": list(unit.sku.flags) if unit.sku else [],
        "ean": unit.ean,
        "is_new": unit.is_new,
        "guarantee": list(unit.warranty),
        "unit_in_stock": unit.in_stock,
        "unit_quantity": unit.quantity,
    }


def _flight(html: str) -> Flight:
    try:
        return Flight.from_html(html)
    except FlightError as err:
        raise ParseError(str(err)) from err
