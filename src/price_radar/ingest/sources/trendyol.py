"""Trendyol adapter: product discovery from two iPhone categories, offers from the product pages.

Trendyol is the project's only source of new iPhones as well as a source of refurbished ones (decision of
12 October 2026), so it emits offers for both; the product page decides which a product is.

Trendyol's mobile web pages carry their data in server-rendered state blocks, ``<script>window["<name>"]={...}``,
written as strict JSON. A category page has ``window["__single-search-result__PROPS"]`` (``data.total``,
``data.pageIndex`` and 36 cards a page), the product page ``window["__envoy-mobile__SHARED_PROPS"]`` (``product`` with
its attributes and ``merchantListing``). They are internal blocks of the site's front end, not an API, and can change
with any deploy, so every parser checks that the block is there and holds what it needs, and raises ParseError with the
reason otherwise.

Discovery. Two categories are read, each from its bare path (page 1, the form measured in Phase 0) and then
``?pi=N`` until ``N * 36 >= data.total`` or a page has no products: the refurbished Apple phones
(``/apple-yenilenmis-cep-telefonu-x-b101470-c109461``) and the iPhone category
(``/iphone-ios-cep-telefonlari-x-c164462``: new phones and some misfiled refurbished ones). Sponsored cards are
skipped, and so are cards of brands other than Apple (cases, batteries, screen protectors). The cards of both categories
are deduplicated by content id in one set, so a product listed in both is requested once. The ranking drifts while the
pages are read, and on the iPhone category sponsored cards seem to take the place of listed products, so the distinct
organic cards of each category are compared with its ``data.total``; a gap is reported, not repaired. A card URL
carries ``boutiqueId`` and ``merchantId``, which are dropped (the product page lists every merchant). Cards whose title
names a model outside the scope are not requested (decision K21).

Model pages (owner's decision of 13 October 2026). On the iPhone category 12 sponsored cards take the place of
listed products on every full page, so about a third of its products never appear on its pages. To recover them,
the adapter reads page 1 of the model page of every in-scope model whose products the iPhone category listed:
``/{model}-cep-telefonu-x-c103498-a292-v{id}``, the "Cep Telefonu" category filtered by the "Cep Telefonu Modeli"
attribute (292), the form the site links to itself. The model value id and name come from the product pages already
read. The model page holds new and refurbished phones of that model; robots.txt disallows ``?pi=`` there, so only
page 1 is read, and a model page whose ``data.total`` is larger than what it shows is reported as incomplete. Its
cards join the same set of content ids, so every product page is still requested once. A combined iPhone-category
and model form was not seen on the site and is not used. If robots.txt disallows the model pages, none is requested
and the run summary says so.

Classification. A product counts as refurbished when its web category is the refurbished one (109461), its warranty
type says "Yenilenmiş", or its title says "Yenilenmiş" or carries an A/B/C "Kalite" grade; anything else is new. The
card's category is not trusted: it decides nothing.

Offers. The product page names the winning (buybox) merchant in ``merchantListing.merchant`` and ``winnerVariant`` and
the other merchants in ``merchantListing.otherMerchants``, each with its own price and listing id. An offer is emitted
only when its variant has ``inStock`` true and ``stockStatus`` 1: out-of-stock pages still show a price. No quantity is
shown (``runningOutQuantity`` is often 0 for available items), so ``stock_qty`` stays None. A page has been seen to
embed at most 12 other merchants (``OTHER_MERCHANTS_SHOWN``), next to a "TÜM SATICILARI GÖSTER" link to ``/saticilar``,
which robots.txt disallows; a page with that many is reported as possibly cut.

Price rule (owner's decision, the same on every platform: see schemas.py). ``price_tl`` is what a buyer pays today:
``discountedPrice``, the selling price minus the basket promotion Trendyol applies automatically (a percentage, a fixed
amount or both). ``list_price_tl`` is the sticker price (``sellingPrice``), or ``price_tl`` when the page shows none,
and ``promo_pct`` the discount in percent of it. Per-user coupons (``couponApplicablePrice``, app codes) are ignored
and ``originalPrice`` is never used.

Attributes. Model and storage come from the product attributes ("Cep Telefonu Modeli", "Dahili Hafıza"), with the
title as fallback. Colour comes from the title, which names it ("Yıldız Işığı") where the "Renk" attribute is a coarse
web colour ("Beyaz"), with the attribute as fallback. The condition of a new product is "Sıfır"; a new product with a
grade in its title ("A Grade") or a "Kozmetik Durum" gets ``condition_conflict`` and a warning. The condition of a
refurbished product is the "Kozmetik Durum" attribute, else the title grade ("A Kalite", "Outlet C Kalite"); when the
two disagree, ``condition_flags`` holds ``condition_conflict``. The warranty is the raw "Garanti Tipi" attribute: for
new phones it separates Apple Türkiye from importer warranties, which price very differently, so an empty value on a
new product is a warning and the values seen are counted in the run summary; normalising them is left to Phase 1.
Trendyol shows no SIM type and no battery health.

Privacy (K12, K22). The merchant block holds the seller's display name, legal name, tax number and office,
registration number, registered e-mail address and address. A merchant is reduced to its numeric id and a few
non-personal flags; nothing else of it is copied.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from price_radar.ingest import urls
from price_radar.ingest.crawl import CrawlContext
from price_radar.ingest.normalize import (
    as_decimal,
    as_int,
    canonical_color,
    canonical_model,
    clean_text,
    color_in_text,
    fold,
    model_in_text,
    storage_gb,
    storage_in_text,
)
from price_radar.ingest.schemas import RawOffer, error_summary, list_price_and_promo

log = logging.getLogger(__name__)

SOURCE = "trendyol"
PARSER_VERSION = "trendyol-3"  # 3: list_price_tl falls back to price_tl
HOST = "www.trendyol.com"
BASE_URL = f"https://{HOST}"
REFURBISHED = "refurbished"
NEW = "new"
MODEL = "model"  # a card first seen on a model page
# The categories read, in this order. A product is filed under the category whose listing showed it first.
CATEGORY_PATHS = {
    REFURBISHED: "/apple-yenilenmis-cep-telefonu-x-b101470-c109461",  # "Apple Yenilenmiş Cep Telefonu"
    NEW: "/iphone-ios-cep-telefonlari-x-c164462",  # "iPhone IOS Cep Telefonları"
}
REFURBISHED_CATEGORY = 109461  # web category "Yenilenmiş Cep Telefonu"
PHONE_CATEGORY = 103498  # "Cep Telefonu": every phone, new and refurbished; the category of the model pages
MODEL_ATTRIBUTE_ID = 292  # "Cep Telefonu Modeli"
PAGE_SIZE = 36
MAX_LISTING_PAGES = 100  # 3,600 cards a category, about four times the 843 of Phase 0; a larger total is not followed
OTHER_MERCHANTS_SHOWN = 12  # the most other merchants a product page was seen to embed (13 sellers in all)
NEW_CONDITION = "Sıfır"
LISTING_STATE = "__single-search-result__PROPS"
PRODUCT_STATE = "__envoy-mobile__SHARED_PROPS"
_PRODUCT_PATH = rf"/apple/{urls.SLUG}-p-\d+"
_MODEL_PATH = rf"/iphone(?:-[a-z0-9]+)*-cep-telefonu-x-c{PHONE_CATEGORY}-a{MODEL_ATTRIBUTE_ID}-v[1-9][0-9]*"
# Only these three forms are ever requested: the two category listings with ?pi=, page 1 of an iPhone model page (no
# query: robots.txt disallows ?pi= on model pages) and product pages. Sorting (sst=), filters, search (?q=), /sr,
# /saticilar and the apigw API links in the page data all fall outside them.
URL_FORMS = (
    urls.UrlForm("listing", re.compile("(?:" + "|".join(map(re.escape, CATEGORY_PATHS.values())) + ")"), ("pi",)),
    urls.UrlForm("model", re.compile(_MODEL_PATH)),
    urls.UrlForm("product", re.compile(_PRODUCT_PATH)),
)
# The link that opens one merchant's offer, as the site's own "other sellers" links do. Built, never requested.
OFFER_FORMS = (urls.UrlForm("offer", re.compile(_PRODUCT_PATH), ("merchantId",)),)
CONTENT_ID = re.compile(r"-p-(\d+)$")
STATE_BLOCK = r'window\["{name}"\]\s*=\s*'

# Attribute names on the product page (product.attributes[].key.name).
MODEL_ATTRIBUTE = "Cep Telefonu Modeli"
STORAGE_ATTRIBUTE = "Dahili Hafıza"
COLOUR_ATTRIBUTE = "Renk"
CONDITION_ATTRIBUTE = "Kozmetik Durum"
WARRANTY_ATTRIBUTE = "Garanti Tipi"
WARRANTY_PERIOD_ATTRIBUTE = "Garanti Süresi"
# Patterns on folded text (normalize.fold: lower case, no diacritics).
REFURBISHED_WORD = re.compile(r"yenilenmis")  # "Yenilenmiş"
REFURBISHED_TITLE = re.compile(r"yenilenmis|\b[abc] kalite\b")  # the refurbished check: "Yenilenmiş", "A Kalite"
TITLE_GRADE = re.compile(r"\b(?:(?P<outlet>outlet) )?(?P<grade>[abc]) (?:kalite|grade)\b")  # any grade in a title
ATTRIBUTE_GRADE = re.compile(r"(?P<grade>[abc]) seviye\b")
GRADE_WORDS = (("cok iyi", "B"), ("mukemmel", "A"), ("iyi", "C"))  # "Çok İyi" before "İyi"
CONDITION_CONFLICT = "condition_conflict"
BASKET_PROMOTION = "DiscountOnBasket"

# Merchant fields worth keeping. Display and legal names, tax number and office, registration number, registered
# e-mail, address, city and logo are deliberately absent.
MERCHANT_FIELDS = {
    "id": "merchant_id",
    "corporateInvoiceApplicable": "corporate_invoice",
    "codEligible": "cash_on_delivery",
    "freeCargo": "free_cargo",
    "rushDelivery": "rush_delivery",
}
VARIANT_FIELDS = {
    "listingId": "listing_id",
    "itemNumber": "item_number",
    "inStock": "in_stock",
    "stockStatus": "stock_status",
    "sellable": "sellable",
    "isRunningOut": "running_out",
    "runningOutQuantity": "running_out_quantity",
    "maxSaleLimit": "max_sale_limit",
    "fulfilmentType": "fulfilment_type",
    "freeCargo": "free_cargo",
    "rushDeliveryDuration": "rush_delivery_duration",
}


class ParseError(ValueError):
    """The page does not have the structure this parser expects."""


@dataclass(frozen=True)
class ListingCard:
    """A product card of a category listing. ``url`` is the bare product URL; ``title`` is used only to skip
    out-of-scope models before their page is requested; ``category`` is the listing that showed it first
    (``REFURBISHED``, ``NEW`` or ``MODEL``)."""

    content_id: int
    url: str
    title: str
    category: str | None = None


@dataclass(frozen=True)
class ListingPage:
    total: int
    page_index: int | None
    records: int  # entries of data.products, whatever they are
    organic: tuple[int, ...]  # ids of every non-sponsored card, whatever its brand: what data.total counts
    cards: tuple[ListingCard, ...]  # the Apple cards with an allowed URL
    ads: int = 0  # sponsored and other non-product cards
    not_apple: int = 0  # cards of other brands (accessories)
    refused: tuple[tuple[str, str], ...] = ()  # (url from the page, reason) for cards whose URL is not allowed
    skipped: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProductPage:
    content_id: int
    model: str
    refurbished: tuple[str, ...]  # why it counts as refurbished: "category", "warranty", "title"; empty for new ones
    in_stock: bool  # at least one merchant has it in stock
    offers: tuple[RawOffer, ...]
    warranty: str | None = None  # the raw "Garanti Tipi"
    out_of_stock: int = 0  # merchants listed without stock
    condition_conflict: bool = False
    seller_list_at_cap: bool = False  # as many other merchants as a page was seen to show: the list may be cut
    rejected: tuple[str, ...] = ()  # offers that failed validation, with the reason
    warnings: tuple[str, ...] = ()
    model_attribute: tuple[int, str] | None = None  # "Cep Telefonu Modeli" value id and name, for the model page
    list_price_problems: tuple[str, ...] = ()  # implausible sticker prices that were not used, with the reason

    @property
    def kind(self) -> str:
        return REFURBISHED if self.refurbished else NEW


@dataclass(frozen=True)
class Condition:
    """``raw`` is "Sıfır" for a new product; for a refurbished one the attribute value, or the title grade written as
    "<grade> Kalite" when there is no attribute."""

    raw: str
    attribute: str | None
    title_grade: str | None  # "A", "B", "C" or "Outlet C"
    conflict: bool


@dataclass(frozen=True)
class _Seller:
    merchant: dict[str, Any]
    variant: dict[str, Any]
    promotions: tuple[Any, ...]
    is_buybox: bool


def check_url(url: str) -> str:
    """Name of the allowed form of ``url`` ("listing" or "product"); raises ``urls.UrlNotAllowed``."""
    return urls.check(url, HOST, URL_FORMS)


def model_url(value_id: int, name: str) -> str:
    """Page 1 of the model page of a "Cep Telefonu Modeli" value: (1218519, "iPhone 15") gives
    ``https://www.trendyol.com/iphone-15-cep-telefonu-x-c103498-a292-v1218519``, as the site's own links write it."""
    slug = re.sub(r"[^a-z0-9]+", "-", fold(name)).strip("-")
    url = f"{BASE_URL}/{slug}-cep-telefonu-x-c{PHONE_CATEGORY}-a{MODEL_ATTRIBUTE_ID}-v{value_id}"
    if check_url(url) != "model":
        raise urls.UrlNotAllowed(f"{url}: not a model page")
    return url


def listing_url(category: str, page: int) -> str:
    """Page ``page`` of a category (``REFURBISHED`` or ``NEW``): the bare path for page 1, ``?pi=N`` after it."""
    url = f"{BASE_URL}{CATEGORY_PATHS[category]}" + (f"?pi={page}" if page > 1 else "")
    check_url(url)
    return url


def product_url(path: str) -> str:
    """Fetch URL of a product from the path a card gives, e.g.
    ``/apple/yenilenmis-iphone-13-128-gb-mavi-p-900101?boutiqueId=7&merchantId=7001``: the query is dropped."""
    path = path.split("#", 1)[0].split("?", 1)[0].strip().removeprefix(BASE_URL)
    url = f"{BASE_URL}/{path.lstrip('/')}"
    if check_url(url) != "product":
        raise urls.UrlNotAllowed(f"{url}: not a product page")
    return url


def offer_url(product: str, merchant_id: int) -> str:
    """The link that opens this product with ``merchant_id`` as the selected merchant."""
    url = f"{product}?merchantId={merchant_id}"
    urls.check(url, HOST, OFFER_FORMS)
    return url


def content_id_of(url: str) -> int | None:
    match = CONTENT_ID.search(url.split("?", 1)[0])
    return int(match.group(1)) if match else None


def window_state(html: str, name: str) -> Any:
    """The JSON value a page assigns to ``window["<name>"]``; None when the page has no such block."""
    marker = re.search(STATE_BLOCK.format(name=re.escape(name)), html)
    if marker is None:
        return None
    try:
        value, _end = json.JSONDecoder().raw_decode(html, marker.end())
    except json.JSONDecodeError as err:
        raise ParseError(f"unreadable {name} block: {err.msg} at offset {err.pos}") from err
    return value


def parse_listing_page(html: str) -> ListingPage:
    state = window_state(html, LISTING_STATE)
    data = state.get("data") if isinstance(state, dict) else None
    if not isinstance(data, dict):
        raise ParseError(f"no listing data (window[{LISTING_STATE!r}].data); markup changed or not a listing page")
    total, products = as_int(data.get("total")), data.get("products")
    if total is None or not isinstance(products, list):
        raise ParseError("listing data without a valid total or products list")
    if total == 0:
        raise ParseError("the listing reports no products (data.total is 0)")
    organic: list[int] = []
    cards: list[ListingCard] = []
    refused: list[tuple[str, str]] = []
    skipped: list[str] = []
    ads = not_apple = 0
    for record in products:
        if not isinstance(record, dict):
            skipped.append("listing card that is not an object")
            continue
        if record.get("cardType", "PRODUCT") != "PRODUCT":
            ads += 1
            continue
        content_id, path = as_int(record.get("id")), record.get("url")
        if content_id is not None:
            organic.append(content_id)
        if record.get("brand") != "Apple":
            not_apple += 1
            continue
        if content_id is None or not isinstance(path, str):
            skipped.append("listing card without an id or url")
            continue
        try:
            url = product_url(path)
        except urls.UrlNotAllowed as err:
            refused.append((path, str(err)))
            continue
        if content_id_of(url) != content_id:
            skipped.append(f"listing card {content_id} links to product {content_id_of(url)}")
            continue
        cards.append(ListingCard(content_id, url, clean_text(record.get("name"))))
    return ListingPage(
        total,
        as_int(data.get("pageIndex")),
        len(products),
        tuple(organic),
        tuple(cards),
        ads,
        not_apple,
        tuple(refused),
        tuple(skipped),
    )


def parse_product_page(html: str, url: str, *, fetched_at: datetime, snapshot_date: date) -> ProductPage:
    """Offers of one product page, new or refurbished: the buybox merchant plus every other merchant that has it in
    stock.

    ``url`` is the page's final URL (after redirects); without its query it becomes the product URL of the offers.
    """
    state = window_state(html, PRODUCT_STATE)
    product = state.get("product") if isinstance(state, dict) else None
    if not isinstance(product, dict):
        raise ParseError(f"no product data (window[{PRODUCT_STATE!r}].product); markup changed or not a product page")
    content_id = as_int(product.get("id"))
    if content_id is None:
        raise ParseError(f"product without a numeric id: {product.get('id')!r}")
    warnings: list[str] = []
    if (requested := content_id_of(url)) is not None and requested != content_id:
        warnings.append(f"page for product {requested} shows product {content_id}")
    attrs = attributes(product)
    title = clean_text(product.get("name"))
    model = _model(attrs, title, warnings)
    sellers = _sellers(product.get("merchantListing"), warnings)
    if all(_price(seller.variant, key) is None for seller in sellers for key in ("sellingPrice", "discountedPrice")):
        raise ParseError(f"no prices on the page of product {content_id}")
    refurbished = refurbished_reasons(product, attrs, title)
    warranty = attrs.get(WARRANTY_ATTRIBUTE) or None
    if not refurbished and warranty is None:
        warnings.append("new product without a Garanti Tipi")
    others = sum(not seller.is_buybox for seller in sellers)
    at_cap = others >= OTHER_MERCHANTS_SHOWN
    if at_cap:
        warnings.append(f"{others} other merchants listed, as many as a page was seen to show: the list may be cut")
    available = [seller for seller in sellers if _in_stock(seller.variant)]
    out_of_stock = len(sellers) - len(available)
    if not available:
        return ProductPage(
            content_id,
            model,
            refurbished,
            False,
            (),
            warranty,
            out_of_stock,
            seller_list_at_cap=at_cap,
            warnings=tuple(warnings),
            model_attribute=model_attribute(product),
        )

    product_page = url.split("?", 1)[0]
    if _form(product_page) != "product":
        raise ParseError(f"{product_page} is not a product URL")
    storage = storage_gb(clean_text(attrs.get(STORAGE_ATTRIBUTE))) or storage_in_text(title)
    if (title_storage := storage_in_text(title)) and storage != title_storage:
        warnings.append(f"storage attribute says {storage} GB, the title {title_storage} GB")
    colour_attribute = clean_text(attrs.get(COLOUR_ATTRIBUTE))
    colour = color_in_text(title) or canonical_color(colour_attribute) or colour_attribute
    condition = condition_of(attrs, title, new=not refurbished)
    if storage is None or not colour or condition is None:
        raise ParseError(f"product {content_id} has no readable storage, colour or condition")
    if condition.conflict and not refurbished:
        warnings.append(
            f"product classified as new has a grade (title {condition.title_grade!r},"
            f" Kozmetik Durum {condition.attribute!r})"
        )
    build = _OfferBuilder(
        content_id=content_id,
        model=model,
        storage=storage,
        color=colour,
        condition=condition,
        warranty=warranty,
        product_url=product_page,
        product=_product_payload(product, attrs, title, condition, refurbished),
        fetched_at=fetched_at,
        snapshot_date=snapshot_date,
    )
    seen: set[int | None] = set()
    for seller in available:
        merchant_id = as_int(seller.merchant.get("id"))
        if merchant_id in seen:
            warnings.append(f"merchant {merchant_id} is listed twice")
            continue
        seen.add(merchant_id)
        build.add(seller, merchant_id)
    return ProductPage(
        content_id,
        model,
        refurbished,
        True,
        tuple(build.offers),
        warranty,
        out_of_stock,
        condition.conflict,
        at_cap,
        tuple(build.rejected),
        tuple(warnings),
        model_attribute(product),
        tuple(build.list_problems),
    )


def attributes(product: dict[str, Any]) -> dict[str, str]:
    """Product attributes by name, e.g. {"Dahili Hafıza": "128 GB", "Kozmetik Durum": "A seviye-Mükemmel"}."""
    found: dict[str, str] = {}
    for entry in product.get("attributes") or []:
        key = entry.get("key") if isinstance(entry, dict) else None
        value = entry.get("value") if isinstance(entry, dict) else None
        if isinstance(key, dict) and isinstance(value, dict) and isinstance(key.get("name"), str):
            found.setdefault(key["name"], clean_text(value.get("name")))
    return found


def model_attribute(product: dict[str, Any]) -> tuple[int, str] | None:
    """The value id and name of the "Cep Telefonu Modeli" attribute, e.g. (613184, "iPhone 13"); None without it."""
    for entry in product.get("attributes") or []:
        key = entry.get("key") if isinstance(entry, dict) else None
        value = entry.get("value") if isinstance(entry, dict) else None
        if isinstance(key, dict) and isinstance(value, dict) and key.get("name") == MODEL_ATTRIBUTE:
            value_id, name = as_int(value.get("id")), clean_text(value.get("name"))
            return (value_id, name) if value_id and name else None
    return None


def refurbished_reasons(product: dict[str, Any], attrs: dict[str, str], title: str) -> tuple[str, ...]:
    """Why a product counts as refurbished; an empty tuple means it is new."""
    reasons = []
    category = product.get("webCategory")
    if isinstance(category, dict) and as_int(category.get("id")) == REFURBISHED_CATEGORY:
        reasons.append("category")
    if REFURBISHED_WORD.search(fold(attrs.get(WARRANTY_ATTRIBUTE, ""))):
        reasons.append("warranty")
    if REFURBISHED_TITLE.search(fold(title)):
        reasons.append("title")
    return tuple(reasons)


def condition_of(attrs: dict[str, str], title: str, *, new: bool = False) -> Condition | None:
    """The condition of a product. New: "Sıfır", in conflict when the page shows a grade anyway. Refurbished: the
    "Kozmetik Durum" attribute or the grade in the title, None if neither."""
    attribute = attrs.get(CONDITION_ATTRIBUTE) or None
    match = TITLE_GRADE.search(fold(title))
    title_grade = f"{'Outlet ' if match['outlet'] else ''}{match['grade'].upper()}" if match else None
    if new:
        return Condition(NEW_CONDITION, attribute, title_grade, bool(attribute or title_grade))
    if attribute is None and title_grade is None:
        return None
    letter = attribute_grade(attribute)
    conflict = bool(letter and match and letter != match["grade"].upper())
    return Condition(attribute or f"{title_grade} Kalite", attribute, title_grade, conflict)


def attribute_grade(value: str | None) -> str | None:
    """Grade letter of a "Kozmetik Durum" value: "A seviye-Mükemmel" -> "A"; "Çok İyi" alone -> "B"."""
    if not value:
        return None
    text = fold(value)
    if match := ATTRIBUTE_GRADE.match(text):
        return match["grade"].upper()
    return next((letter for words, letter in GRADE_WORDS if words in text), None)


@dataclass
class _OfferBuilder:
    """Turns the merchants of one product page into validated offers; invalid ones become rejections."""

    content_id: int
    model: str
    storage: int
    color: str
    condition: Condition
    warranty: str | None
    product_url: str
    product: dict[str, Any]
    fetched_at: datetime
    snapshot_date: date
    offers: list[RawOffer] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    list_problems: list[str] = field(default_factory=list)

    def add(self, seller: _Seller, merchant_id: int | None) -> None:
        key = f"{SOURCE}:{self.content_id}:{merchant_id}"
        try:
            if merchant_id is None or merchant_id < 1:
                raise ValueError("no merchant id")
            paid = _price(seller.variant, "discountedPrice")
            if paid is None:
                raise ValueError("no discountedPrice")
            listed, promo, problem = list_price_and_promo(paid, _price(seller.variant, "sellingPrice"))
            offer = RawOffer(
                platform=SOURCE,
                offer_key=key,
                snapshot_date=self.snapshot_date,
                fetched_at=self.fetched_at,
                model=self.model,
                storage_gb=self.storage,
                color=self.color,
                condition_raw=self.condition.raw,
                sim_type=None,
                warranty_raw=self.warranty,
                condition_flags=[CONDITION_CONFLICT] if self.condition.conflict else [],
                seller_id=merchant_id,
                is_buybox=seller.is_buybox,
                price_tl=paid,
                list_price_tl=listed,
                promo_pct=promo,
                stock_qty=None,
                in_stock=True,
                product_url=self.product_url,
                offer_url=offer_url(self.product_url, merchant_id),
                payload={"product": self.product, "offer": _offer_payload(seller), "seller": _seller_payload(seller)},
                parser_version=PARSER_VERSION,
            )
        except (ValidationError, ValueError) as err:
            self.rejected.append(f"{key}: {error_summary(err)}")
            return
        self.offers.append(offer)
        if problem:
            self.list_problems.append(f"{key}: {problem}")


@dataclass
class _Limit:
    """Lets a card's product page be requested unless its title names an out-of-scope model or the page limit
    (``--max-variants``) is reached; counts what it holds back."""

    ctx: CrawlContext
    fetched: int = 0

    def allows(self, card: ListingCard) -> bool:
        title_model = model_in_text(card.title)
        if title_model is not None and not self.ctx.scope.includes(title_model):
            self.ctx.stats.counters["cards_out_of_scope"] += 1  # never requested (decision K21)
            return False
        if self.ctx.max_variants is not None and self.fetched >= self.ctx.max_variants:
            self.ctx.stats.counters["products_over_limit"] += 1
            return False
        self.fetched += 1
        return True


def crawl(ctx: CrawlContext) -> Iterator[RawOffer]:
    """Read both category listings and yield the offers of each in-scope product page they list; then read page 1 of
    the model page of each in-scope model the iPhone category listed, and yield the offers of the products only the
    model pages showed.

    The listings are read in one pass before any product page, so their pages are read close together (less drift).
    """
    found, listed_in_new = _discover(ctx)
    models: dict[int, str] = {}  # "Cep Telefonu Modeli" value id -> name, from products the iPhone category listed
    limit = _Limit(ctx)
    try:
        for card in list(found.values()):
            if limit.allows(card):
                yield from _product_offers(ctx, card, models if card.content_id in listed_in_new else None)
        for card in _read_model_listings(ctx, models, found):
            if limit.allows(card):
                yield from _product_offers(ctx, card)
    finally:
        _record_coverage(ctx)


def _discover(ctx: CrawlContext) -> tuple[dict[int, ListingCard], set[int]]:
    """Distinct Apple cards of both categories by content id, in listing order, each with the category that showed
    it first, and the ids the iPhone category listed."""
    found: dict[int, ListingCard] = {}
    categories: dict[str, dict[str, Any]] = {}
    listed: dict[str, set[int]] = {}
    for category in CATEGORY_PATHS:
        categories[category], listed[category] = _read_category(ctx, category, found)
    ctx.stats.discovery.update(categories=categories, products_seen=len(found))
    ctx.stats.counters["variants_discovered"] += len(found)  # a Trendyol product is the counterpart of a variant
    return found, listed[NEW]


def _read_category(ctx: CrawlContext, category: str, found: dict[int, ListingCard]) -> tuple[dict[str, Any], set[int]]:
    """Read one category's listing into ``found``; returns what the reading saw and the ids it listed."""
    organic: set[int] = set()
    cards: dict[int, ListingCard] = {}
    totals: set[int] = set()
    total: int | None = None
    number = failed = organic_seen = ads = not_apple = 0
    more = True
    while more:
        number += 1
        page = _fetch_listing(ctx, listing_url(category, number), "listing", number)
        if page is None:
            failed += 1
            more = total is not None and number * PAGE_SIZE < total  # without page 1 there is no total to go by
        else:
            total = page.total
            totals.add(total)
            organic_seen += len(page.organic)
            organic.update(page.organic)
            ads += page.ads
            not_apple += page.not_apple
            for card in page.cards:
                cards.setdefault(card.content_id, dataclasses.replace(card, category=category))
            more = page.records > 0 and number * PAGE_SIZE < total
        if more and number >= MAX_LISTING_PAGES:
            ctx.stats.warn(f"{category} listing: data.total is {total}; stopped after {MAX_LISTING_PAGES} pages")
            break
    listed_before = sum(1 for content_id in cards if content_id in found)
    for content_id, card in cards.items():
        found.setdefault(content_id, card)
    ctx.stats.counters[f"{category}_products_discovered"] += len(cards) - listed_before
    if not_apple:
        ctx.stats.counters["cards_not_apple"] += not_apple
    if listed_before:
        ctx.stats.counters["products_in_both_listings"] += listed_before
    if total is not None and len(organic) != total:
        ctx.stats.warn(
            f"{category} listing: data.total is {total} but {len(organic)} distinct cards were listed"
            f" ({organic_seen} cards, {ads} sponsored, {failed} failed pages); failed pages, ranking drift and"
            " sponsored cards leave products out"
        )
    record = {
        "listing_totals": sorted(totals),
        "listing_pages": number,
        "listing_pages_failed": failed,
        "cards_seen": organic_seen,
        "duplicate_cards": organic_seen - len(organic),
        "distinct_cards": len(organic),
        "missing": max(0, (total or 0) - len(organic)),
        "ads_skipped": ads,
        "not_apple_skipped": not_apple,
        "products_seen": len(cards),
        "listed_in_an_earlier_category": listed_before,
    }
    return record, set(cards)


def _read_model_listings(ctx: CrawlContext, models: dict[int, str], found: dict[int, ListingCard]) -> list[ListingCard]:
    """Page 1 of the model page of each model in ``models``; the Apple cards not seen before join ``found`` and are
    returned."""
    counters = ctx.stats.counters
    added: list[ListingCard] = []
    records: list[dict[str, Any]] = []
    for value_id, name in models.items():
        try:
            url = model_url(value_id, name)
        except urls.UrlNotAllowed as err:
            ctx.stats.warn(f"no model page for {name!r}: {err}")
            continue
        if not ctx.client.allowed(url):
            counters["model_listings_refused"] += 1
            ctx.stats.refuse("model", url, "robots.txt")
            ctx.stats.warn(
                "robots.txt disallows the model pages: the products that sponsored cards hide in the iPhone category"
                " are not recovered in this run"
            )
            break
        counters["model_listings_requested"] += 1
        page = _fetch_listing(ctx, url, "model")
        if page is None:
            records.append({"model": name, "read": False})
            continue
        shown = len(set(page.organic))
        new_cards = [card for card in page.cards if card.content_id not in found]
        for card in new_cards:
            found[card.content_id] = dataclasses.replace(card, category=MODEL)
            added.append(found[card.content_id])
        incomplete = page.total > shown
        if incomplete:
            counters["model_listings_incomplete"] += 1
            ctx.stats.warn(
                f"model page of {name}: data.total is {page.total} but page 1 shows {shown} products and ?pi= is"
                " disallowed there; the model may be incomplete"
            )
        records.append(
            {
                "model": name,
                "read": True,
                "total": page.total,
                "shown": shown,
                "ads_skipped": page.ads,
                "products_added": len({card.content_id for card in new_cards}),
                "incomplete": incomplete,
            }
        )
    if added:
        counters[f"{MODEL}_products_discovered"] += len(added)
        counters["variants_discovered"] += len(added)
    ctx.stats.discovery["model_listings"] = {"models": records, "products_added": len(added)}
    return added


def _record_coverage(ctx: CrawlContext) -> None:
    """How much of the iPhone category's data.total the run saw: on its own pages, and added by the model pages."""
    new = ctx.stats.discovery.get("categories", {}).get(NEW)
    if not new or not new["listing_totals"]:
        return
    total = new["listing_totals"][-1]
    counters = ctx.stats.counters
    added_new = counters[f"{NEW}_in_{MODEL}_listing"]
    ctx.stats.discovery["new_category_coverage"] = {
        "data_total": total,
        "seen_on_category_pages": new["distinct_cards"],
        "added_by_model_pages": counters[f"{MODEL}_products_discovered"],
        "added_by_model_pages_and_new": added_new,
        "remaining_gap": max(0, total - new["distinct_cards"] - added_new),
    }


def _fetch_listing(ctx: CrawlContext, url: str, kind: str, number: int = 1) -> ListingPage | None:
    response = ctx.get(url, kind)
    if response is None:
        return None
    if response.status_code != 200:
        ctx.stats.fail(kind, url, f"HTTP {response.status_code}")
        return None
    try:
        page = parse_listing_page(response.text)
    except ParseError as err:
        ctx.stats.fail(kind, url, str(err))
        return None
    for path, reason in page.refused:
        ctx.stats.refuse("product", path, reason)
    for reason in page.skipped:
        ctx.stats.warn(f"{url}: {reason}")
    if page.page_index is not None and page.page_index != number:
        ctx.stats.warn(f"{url}: data.pageIndex is {page.page_index}")
    return page


def _product_offers(ctx: CrawlContext, card: ListingCard, models: dict[int, str] | None = None) -> Iterator[RawOffer]:
    """Yield the offers of one card's product page; with ``models``, also note the page's model for its model page."""
    counters = ctx.stats.counters
    counters[f"{card.category}_pages_requested"] += 1
    response = ctx.get(card.url, "product")
    if response is None:
        return
    if response.status_code in (404, 410):
        # The listing lags the product pages, so a listed product can already be gone.
        counters["products_removed"] += 1
        ctx.stats.warn(f"product {card.content_id} answered HTTP {response.status_code} (removed)")
        return
    if response.status_code != 200:
        ctx.stats.fail("product", card.url, f"HTTP {response.status_code}")
        return
    final_url = str(response.url)
    if _form(final_url.split("?", 1)[0]) != "product":
        counters["products_redirected_away"] += 1
        return
    try:
        page = parse_product_page(response.text, final_url, fetched_at=ctx.now(), snapshot_date=ctx.snapshot_date)
    except ParseError as err:
        ctx.stats.fail("product", card.url, str(err))
        return
    for warning in page.warnings:
        ctx.stats.warn(f"product {page.content_id}: {warning}")
    for reason in page.rejected:
        counters["offers_rejected"] += 1
        ctx.stats.warn(f"rejected offer {reason}")
    for problem in page.list_price_problems:
        counters["implausible_list_prices"] += 1
        ctx.stats.warn(f"implausible list price not used: {problem}")
    if not ctx.scope.includes(page.model):
        counters["products_out_of_scope"] += 1
        return
    if models is not None and page.model_attribute is not None:
        models.setdefault(*page.model_attribute)
    kind = page.kind
    counters[f"{kind}_products"] += 1
    if page.warranty is not None:
        ctx.stats.observe(f"warranty_types_{kind}", page.warranty)
    elif kind == NEW:
        counters["new_without_warranty_type"] += 1
    if card.category not in (None, kind):
        counters[f"{kind}_in_{card.category}_listing"] += 1  # a misfiled product, by the product page
    if kind == REFURBISHED and "category" not in page.refurbished:
        counters["refurbished_outside_category"] += 1
    if page.seller_list_at_cap:
        counters["seller_lists_at_cap"] += 1
    if page.out_of_stock:
        counters["offers_out_of_stock"] += page.out_of_stock
    if not page.in_stock:
        counters["products_out_of_stock"] += 1
        return
    if page.condition_conflict:
        counters["condition_conflicts"] += 1
    counters[f"{kind}_offers"] += len(page.offers)
    ctx.stats.sellers_per_variant[len(page.offers)] += 1
    yield from page.offers


def _model(attrs: dict[str, str], title: str, warnings: list[str]) -> str:
    from_attribute = canonical_model(attrs.get(MODEL_ATTRIBUTE))
    from_title = model_in_text(title)
    if from_attribute and from_title and from_attribute != from_title:
        warnings.append(f"model attribute says {from_attribute}, the title {from_title}")
    model = from_attribute or from_title
    if model is None:
        raise ParseError(f"unknown model (attribute {attrs.get(MODEL_ATTRIBUTE)!r}, none in the title)")
    return model


def _sellers(listing: Any, warnings: list[str]) -> list[_Seller]:
    """The buybox merchant, then every other merchant with its variant of this product."""
    merchant = listing.get("merchant") if isinstance(listing, dict) else None
    winner = listing.get("winnerVariant") if isinstance(listing, dict) else None
    if not isinstance(merchant, dict) or not isinstance(winner, dict):
        raise ParseError("no merchantListing with a merchant and a winnerVariant")
    sellers = [_Seller(merchant, winner, tuple(_items(listing.get("promotions"))), True)]
    others = listing.get("otherMerchants")
    if others is not None and not isinstance(others, list):
        warnings.append("otherMerchants is not a list")
    for entry in others if isinstance(others, list) else []:
        variants = [v for v in _items(entry.get("variants")) if isinstance(v, dict)] if isinstance(entry, dict) else []
        if not variants:
            warnings.append("other merchant without a variant")
            continue
        if len(variants) > 1:
            warnings.append(f"merchant {entry.get('id')} lists {len(variants)} variants")
        variant = next((v for v in variants if v.get("itemNumber") == winner.get("itemNumber")), variants[0])
        sellers.append(_Seller(entry, variant, tuple(_items(entry.get("promotions"))), False))
    return sellers


def _in_stock(variant: dict[str, Any]) -> bool:
    return variant.get("inStock") is True and as_int(variant.get("stockStatus")) == 1


def _price(variant: dict[str, Any], key: str) -> Decimal | None:
    price = variant.get("price") if isinstance(variant.get("price"), dict) else {}
    node = price.get(key)
    return as_decimal(node.get("value")) if isinstance(node, dict) else None


def _product_payload(
    product: dict[str, Any], attrs: dict[str, str], title: str, condition: Condition, refurbished: tuple[str, ...]
) -> dict[str, Any]:
    web_category = product.get("webCategory") if isinstance(product.get("webCategory"), dict) else {}
    category = product.get("category") if isinstance(product.get("category"), dict) else {}
    return {
        "content_id": product.get("id"),
        "product_group_id": product.get("productGroupId"),
        "title": title,
        "classified_as": REFURBISHED if refurbished else NEW,
        "refurbished_by": list(refurbished),
        "web_category_id": web_category.get("id"),
        "category_id": category.get("id"),
        "model_raw": attrs.get(MODEL_ATTRIBUTE),
        "storage_raw": attrs.get(STORAGE_ATTRIBUTE),
        "color_raw": attrs.get(COLOUR_ATTRIBUTE),
        "color_in_title": color_in_text(title),
        "condition_attribute": condition.attribute,
        "title_grade": condition.title_grade,
        "warranty_type": attrs.get(WARRANTY_ATTRIBUTE),
        "warranty_period": attrs.get(WARRANTY_PERIOD_ATTRIBUTE),
    }


def _offer_payload(seller: _Seller) -> dict[str, Any]:
    price = seller.variant.get("price") if isinstance(seller.variant.get("price"), dict) else {}
    raw = {key: node.get("value") for key, node in price.items() if isinstance(node, dict)}
    return _rename(seller.variant, VARIANT_FIELDS) | {
        "selling_price": raw.get("sellingPrice"),
        "discounted_price": raw.get("discountedPrice"),
        "basket_promotions": [
            {
                "promotion_id": promotion.get("id"),
                "end_date": promotion.get("promotionEndDate"),
                "applied": promotion.get("isApplied"),
            }
            for promotion in seller.promotions
            if isinstance(promotion, dict) and promotion.get("promotionDiscountType") == BASKET_PROMOTION
        ],
    }


def _seller_payload(seller: _Seller) -> dict[str, Any]:
    merchant = seller.merchant
    score = merchant.get("sellerScore")
    badges = [
        badge["type"]
        for key in ("merchantBadges", "merchantMarkers")
        for badge in _items(merchant.get(key))
        if isinstance(badge, dict) and isinstance(badge.get("type"), str)
    ]
    return _rename(merchant, MERCHANT_FIELDS) | {
        "seller_score": score.get("value") if isinstance(score, dict) else None,
        "badges": badges,
    }


def _form(url: str) -> str | None:
    try:
        return check_url(url)
    except urls.UrlNotAllowed:
        return None


def _items(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _rename(record: dict[str, Any], fields: dict[str, str]) -> dict[str, Any]:
    return {new: record.get(old) for old, new in fields.items() if old in record}
