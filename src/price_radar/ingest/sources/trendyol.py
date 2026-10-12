"""Trendyol adapter: product discovery from the refurbished-Apple category, offers from the product pages.

Trendyol's mobile web pages carry their data in server-rendered state blocks, ``<script>window["<name>"]={...}``,
written as strict JSON. The category page has ``window["__single-search-result__PROPS"]`` (``data.total``,
``data.pageIndex`` and 36 product cards a page), the product page ``window["__envoy-mobile__SHARED_PROPS"]``
(``product`` with its attributes and ``merchantListing``). They are internal blocks of the site's front end, not an
API, and can change with any deploy, so every parser checks that the block is there and holds what it needs, and
raises ParseError with the reason otherwise.

Discovery. ``/apple-yenilenmis-cep-telefonu-x-b101470-c109461`` lists the refurbished Apple phones. Page 1 is the bare
path (the form measured in Phase 0), page N is ``?pi=N``; reading stops when ``N * 36 >= data.total`` or a page has no
products. The ranking drifts while the pages are read (a card can appear twice, another not at all), so cards are
deduplicated by content id and the distinct count is compared with ``data.total``; a gap is reported, not repaired.
Cards only give content ids: a card URL carries ``boutiqueId`` and ``merchantId``, which are dropped (the product page
lists every merchant), and the card's category is not trusted (refurbished products are sometimes filed under "IOS Cep
Telefonları"). Cards whose title names a model outside the scope are not requested (decision K21).

Offers. The product page names the winning (buybox) merchant in ``merchantListing.merchant`` and ``winnerVariant`` and
every other merchant in ``merchantListing.otherMerchants``, each with its own price and listing id. An offer is
emitted only when its variant has ``inStock`` true and ``stockStatus`` 1: out-of-stock pages still show a price. No
quantity is shown (``runningOutQuantity`` is often 0 for available items), so ``stock_qty`` stays None.

Price rule (owner's decision). ``price_tl`` is what a buyer pays today: ``discountedPrice``, the selling price minus
the basket promotion Trendyol applies automatically (a percentage, a fixed amount or both). ``list_price_tl`` is the
sticker price (``sellingPrice``) and ``promo_pct`` the resulting discount in percent of it. Per-user coupons
(``couponApplicablePrice``, app codes) are ignored and ``originalPrice`` is never used.

Attributes. Model and storage come from the product attributes ("Cep Telefonu Modeli", "Dahili Hafıza"), with the
title as fallback. Colour comes from the title, which names it ("Yıldız Işığı") where the "Renk" attribute is a coarse
web colour ("Beyaz"), with the attribute as fallback. Condition is the "Kozmetik Durum" attribute, else the title grade
("A Kalite", "Outlet C Kalite"); when the two disagree, ``condition_flags`` holds ``condition_conflict``. Trendyol shows
no SIM type and no battery health; the warranty is the "Garanti Tipi" attribute.

Refurbished check. A product counts as refurbished when its web category is the refurbished one (109461), its
warranty type says "Yenilenmiş", or its title says "Yenilenmiş" or carries an A/B/C grade. Anything else is a new
phone: it is skipped and counted.

Privacy (K12, K22). The merchant block holds the seller's display name, legal name, tax number and office,
registration number, registered e-mail address and address. A merchant is reduced to its numeric id and a few
non-personal flags; nothing else of it is copied.
"""

from __future__ import annotations

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
from price_radar.ingest.schemas import CENTS, RawOffer, error_summary

log = logging.getLogger(__name__)

SOURCE = "trendyol"
PARSER_VERSION = "trendyol-1"
HOST = "www.trendyol.com"
BASE_URL = f"https://{HOST}"
CATEGORY_PATH = "/apple-yenilenmis-cep-telefonu-x-b101470-c109461"
REFURBISHED_CATEGORY = 109461  # web category "Yenilenmiş Cep Telefonu"
PAGE_SIZE = 36
MAX_LISTING_PAGES = 100  # 3,600 products, about four times the 843 of Phase 0; a larger data.total is not followed
LISTING_STATE = "__single-search-result__PROPS"
PRODUCT_STATE = "__envoy-mobile__SHARED_PROPS"
_PRODUCT_PATH = rf"/apple/{urls.SLUG}-p-\d+"
# Only these two forms are ever requested. Per-model listings (whose ?pi= robots.txt disallows), /saticilar, /sr,
# sorting (sst=), search (?q=) and the apigw API links in the page data all fall outside them.
URL_FORMS = (
    urls.UrlForm("listing", re.compile(re.escape(CATEGORY_PATH)), ("pi",)),
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
REFURBISHED_WORD = re.compile(r"yenilenmis")
TITLE_GRADE = re.compile(r"\b(?:(?P<outlet>outlet) )?(?P<grade>[abc]) (?:kalite|grade)\b")
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
    """A product card of the category listing. ``url`` is the bare product URL; ``title`` is used only to skip
    out-of-scope models before their page is requested."""

    content_id: int
    url: str
    title: str


@dataclass(frozen=True)
class ListingPage:
    total: int
    page_index: int | None
    records: int  # entries of data.products, whatever they are
    cards: tuple[ListingCard, ...]
    ads: int = 0  # sponsored and other non-product cards; they are not part of data.total
    refused: tuple[tuple[str, str], ...] = ()  # (url from the page, reason) for cards whose URL is not allowed
    skipped: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProductPage:
    content_id: int
    model: str
    refurbished: tuple[str, ...]  # why it counts as refurbished: "category", "warranty", "title"; empty for new phones
    in_stock: bool  # at least one merchant has it in stock
    offers: tuple[RawOffer, ...]
    out_of_stock: int = 0  # merchants listed without stock
    condition_conflict: bool = False
    rejected: tuple[str, ...] = ()  # offers that failed validation, with the reason
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Condition:
    """``raw`` is the attribute value, or the title grade written as "<grade> Kalite" when there is no attribute."""

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


def listing_url(page: int) -> str:
    url = f"{BASE_URL}{CATEGORY_PATH}" + (f"?pi={page}" if page > 1 else "")
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
    cards: list[ListingCard] = []
    refused: list[tuple[str, str]] = []
    skipped: list[str] = []
    ads = 0
    for record in products:
        if not isinstance(record, dict):
            skipped.append("listing card that is not an object")
            continue
        if record.get("cardType", "PRODUCT") != "PRODUCT":
            ads += 1
            continue
        content_id, path = as_int(record.get("id")), record.get("url")
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
        total, as_int(data.get("pageIndex")), len(products), tuple(cards), ads, tuple(refused), tuple(skipped)
    )


def parse_product_page(html: str, url: str, *, fetched_at: datetime, snapshot_date: date) -> ProductPage:
    """Offers of one product page: the buybox merchant plus every other merchant that has it in stock.

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
    available = [seller for seller in sellers if _in_stock(seller.variant)]
    out_of_stock = len(sellers) - len(available)
    if not refurbished or not available:
        return ProductPage(content_id, model, refurbished, bool(available), (), out_of_stock, warnings=tuple(warnings))

    product_page = url.split("?", 1)[0]
    if _form(product_page) != "product":
        raise ParseError(f"{product_page} is not a product URL")
    storage = storage_gb(clean_text(attrs.get(STORAGE_ATTRIBUTE))) or storage_in_text(title)
    if (title_storage := storage_in_text(title)) and storage != title_storage:
        warnings.append(f"storage attribute says {storage} GB, the title {title_storage} GB")
    colour_attribute = clean_text(attrs.get(COLOUR_ATTRIBUTE))
    colour = color_in_text(title) or canonical_color(colour_attribute) or colour_attribute
    condition = condition_of(attrs, title)
    if storage is None or not colour or condition is None:
        raise ParseError(f"product {content_id} has no readable storage, colour or condition")
    build = _OfferBuilder(
        content_id=content_id,
        model=model,
        storage=storage,
        color=colour,
        condition=condition,
        warranty=clean_text(attrs.get(WARRANTY_ATTRIBUTE)) or None,
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
        out_of_stock,
        condition.conflict,
        tuple(build.rejected),
        tuple(warnings),
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


def refurbished_reasons(product: dict[str, Any], attrs: dict[str, str], title: str) -> tuple[str, ...]:
    reasons = []
    category = product.get("webCategory")
    if isinstance(category, dict) and as_int(category.get("id")) == REFURBISHED_CATEGORY:
        reasons.append("category")
    if REFURBISHED_WORD.search(fold(attrs.get(WARRANTY_ATTRIBUTE, ""))):
        reasons.append("warranty")
    folded = fold(title)
    if REFURBISHED_WORD.search(folded) or TITLE_GRADE.search(folded):
        reasons.append("title")
    return tuple(reasons)


def condition_of(attrs: dict[str, str], title: str) -> Condition | None:
    """The cosmetic condition from the "Kozmetik Durum" attribute and the grade in the title, or None if neither."""
    attribute = attrs.get(CONDITION_ATTRIBUTE) or None
    match = TITLE_GRADE.search(fold(title))
    title_grade = f"{'Outlet ' if match['outlet'] else ''}{match['grade'].upper()}" if match else None
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


def promo_pct(list_price: Decimal, paid: Decimal) -> Decimal | None:
    """The basket promotion in percent of the sticker price; None when the price paid is above it (an error)."""
    if paid > list_price:
        return None
    return ((list_price - paid) / list_price * 100).quantize(CENTS)


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

    def add(self, seller: _Seller, merchant_id: int | None) -> None:
        key = f"{SOURCE}:{self.content_id}:{merchant_id}"
        try:
            if merchant_id is None or merchant_id < 1:
                raise ValueError("no merchant id")
            listed = _price(seller.variant, "sellingPrice")
            paid = _price(seller.variant, "discountedPrice")
            if listed is None or paid is None:
                raise ValueError("no sellingPrice or discountedPrice")
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
                promo_pct=promo_pct(listed, paid),
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


def crawl(ctx: CrawlContext) -> Iterator[RawOffer]:
    """Read the category listing, then yield the offers of each in-scope product page it lists.

    The listing is read in one pass before any product page, so the pages are read close together (less drift).
    """
    cards = _discover(ctx)
    fetched = 0
    for card in cards.values():
        title_model = model_in_text(card.title)
        if title_model is not None and not ctx.scope.includes(title_model):
            ctx.stats.counters["cards_out_of_scope"] += 1  # never requested (decision K21)
            continue
        if ctx.max_variants is not None and fetched >= ctx.max_variants:
            ctx.stats.counters["products_over_limit"] += 1
            continue
        fetched += 1
        yield from _product_offers(ctx, card)


def _discover(ctx: CrawlContext) -> dict[int, ListingCard]:
    """Distinct cards of the category listing by content id, in listing order."""
    found: dict[int, ListingCard] = {}
    totals: set[int] = set()
    total: int | None = None
    number = failed = cards_seen = ads = 0
    more = True
    while more:
        number += 1
        page = _fetch_listing(ctx, number)
        if page is None:
            failed += 1
            more = total is not None and number * PAGE_SIZE < total  # without page 1 there is no total to go by
        else:
            total = page.total
            totals.add(total)
            cards_seen += len(page.cards)
            ads += page.ads
            for card in page.cards:
                found.setdefault(card.content_id, card)
            more = page.records > 0 and number * PAGE_SIZE < total
        if more and number >= MAX_LISTING_PAGES:
            ctx.stats.warn(f"listing: data.total is {total}; stopped after {MAX_LISTING_PAGES} pages")
            break
    distinct = len(found)
    ctx.stats.discovery.update(
        listing_totals=sorted(totals),
        listing_pages=number,
        listing_pages_failed=failed,
        cards_seen=cards_seen,
        duplicate_cards=cards_seen - distinct,
        ads_skipped=ads,
        products_seen=distinct,
        missing=max(0, (total or 0) - distinct),
    )
    ctx.stats.counters["variants_discovered"] += distinct  # a Trendyol product is the counterpart of a variant
    if total is not None and distinct != total:
        ctx.stats.warn(
            f"listing: data.total is {total} but {distinct} distinct products were listed ({cards_seen} cards,"
            f" {failed} failed pages); failed pages and ranking drift leave products out"
        )
    return found


def _fetch_listing(ctx: CrawlContext, number: int) -> ListingPage | None:
    url = listing_url(number)
    response = ctx.get(url, "listing")
    if response is None:
        return None
    if response.status_code != 200:
        ctx.stats.fail("listing", url, f"HTTP {response.status_code}")
        return None
    try:
        page = parse_listing_page(response.text)
    except ParseError as err:
        ctx.stats.fail("listing", url, str(err))
        return None
    for path, reason in page.refused:
        ctx.stats.refuse("product", path, reason)
    for reason in page.skipped:
        ctx.stats.warn(f"{url}: {reason}")
    if page.page_index is not None and page.page_index != number:
        ctx.stats.warn(f"{url}: data.pageIndex is {page.page_index}")
    return page


def _product_offers(ctx: CrawlContext, card: ListingCard) -> Iterator[RawOffer]:
    response = ctx.get(card.url, "product")
    if response is None:
        return
    if response.status_code in (404, 410):
        # The listing lags the product pages, so a listed product can already be gone.
        ctx.stats.counters["products_removed"] += 1
        ctx.stats.warn(f"product {card.content_id} answered HTTP {response.status_code} (removed)")
        return
    if response.status_code != 200:
        ctx.stats.fail("product", card.url, f"HTTP {response.status_code}")
        return
    final_url = str(response.url)
    if _form(final_url.split("?", 1)[0]) != "product":
        ctx.stats.counters["products_redirected_away"] += 1
        return
    try:
        page = parse_product_page(response.text, final_url, fetched_at=ctx.now(), snapshot_date=ctx.snapshot_date)
    except ParseError as err:
        ctx.stats.fail("product", card.url, str(err))
        return
    for warning in page.warnings:
        ctx.stats.warn(f"product {page.content_id}: {warning}")
    for reason in page.rejected:
        ctx.stats.counters["offers_rejected"] += 1
        ctx.stats.warn(f"rejected offer {reason}")
    if not page.refurbished:
        ctx.stats.counters["products_not_refurbished"] += 1
        return
    if not ctx.scope.includes(page.model):
        ctx.stats.counters["products_out_of_scope"] += 1
        return
    if page.out_of_stock:
        ctx.stats.counters["offers_out_of_stock"] += page.out_of_stock
    if not page.in_stock:
        ctx.stats.counters["products_out_of_stock"] += 1
        return
    if "category" not in page.refurbished:
        ctx.stats.counters["products_refurbished_outside_category"] += 1
    if page.condition_conflict:
        ctx.stats.counters["condition_conflicts"] += 1
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
        "refurbished_by": list(refurbished),
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
