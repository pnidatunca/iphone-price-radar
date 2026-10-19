"""Getmobil adapter: variant discovery from the model listings, offers from the variant pages.

Discovery. The Apple brand page lists the model categories in its RSC data (``data.aggregations.models``). Each
in-scope model is paged through with ``?pageNumber=N`` up to ``data.lastPage``; every record of a model listing is one
in-stock variant (model x storage x colour x condition, x SIM type on some models) with its own page. The listing comes
from a search index that lags the product pages and its order drifts while it is read, so it is used only to find
variant ids: records are deduplicated by id, and a model whose distinct ids do not add up to ``data.total`` is read
once more.

Offers. A variant page names the selected (buybox) seller in ``priceInfo``/``stockInfo``/``cartInfo`` and every other
seller in ``alternativeSellersData.sellers``. JSON-LD is not used: its offers hold at most the ten cheapest
alternatives and leave out the buybox seller, which is often Getmobil itself.

Prices (the rule of schemas.py). ``price_tl`` is the seller's price (``priceInfo.main`` for the buybox). The buybox
sometimes shows a struck-through earlier price (``priceInfo.strikethrough``), which becomes ``list_price_tl``; other
sellers show none, so for them, and for a buybox without one, ``list_price_tl`` is ``price_tl``.

Phase 0 traps this module avoids:

* ``rel=canonical`` of a non-Mükemmel variant points to its Mükemmel sibling and URL slugs can name the wrong storage
  or colour, so identity comes from ``cartInfo`` and the attributes from the variant view;
* an out-of-stock page still answers 200 with the last price ("Stokta Yok") and a placeholder seller; it yields no
  offer;
* seller display names, vendor slugs and ``/magazalarimiz/`` store links can contain personal names; they are never
  copied into an offer, only the numeric vendor id is.
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
from price_radar.ingest.normalize import as_decimal, as_int, canonical_model, clean_text, storage_gb
from price_radar.ingest.rsc import Flight, FlightError
from price_radar.ingest.schemas import RawOffer, error_summary, list_price_and_promo

log = logging.getLogger(__name__)

SOURCE = "getmobil"
PARSER_VERSION = "getmobil-2"  # 2: list_price_tl and promo_pct filled
HOST = "getmobil.com"
BASE_URL = f"https://{HOST}"
CATALOG = "/satin-al/cep-telefonu/apple"
_VARIANT_PATH = re.escape(CATALOG) + rf"/{urls.SLUG}/{urls.SLUG}-\d+/"
URL_FORMS = (
    urls.UrlForm("brand", re.compile(re.escape(CATALOG) + "/")),
    urls.UrlForm("listing", re.compile(re.escape(CATALOG) + rf"/{urls.SLUG}/"), ("pageNumber",)),
    urls.UrlForm("variant", re.compile(_VARIANT_PATH)),
)
# The link a buyer follows to one seller's offer. It is built, never requested.
OFFER_FORMS = (urls.UrlForm("offer", re.compile(_VARIANT_PATH), ("merchantId",)),)
VARIANT_ID = re.compile(r"-(\d+)/$")
SELLER_LABEL = re.compile(r"Tüm satıcıları gör(?:<!-- -->)?\s*\((?:<!-- -->)?(\d+)")
# Seller fields worth keeping; names, slugs and store links are deliberately absent.
SELLER_FIELDS = {
    "vendorId": "vendor_id",
    "inventoryId": "inventory_id",
    "price": "price",
    "stock": "stock",
    "isGetmobil": "is_getmobil",
    "isFastDelivery": "fast_delivery",
    "shippingDays": "shipping_days",
    "score": "score",
    "isTopSeller": "top_seller",
    "isTrustedSeller": "trusted_seller",
    "isVendorOfTheMonth": "vendor_of_the_month",
    "isSecondHandPhone": "second_hand_phone",
    "isBuyboxWinner": "buybox_winner_flag",
}
VENDOR_FIELDS = {
    "id": "vendor_id",
    "isGetmobil": "is_getmobil",
    "score": "score",
    "isTopSeller": "top_seller",
    "isTrustedSeller": "trusted_seller",
    "isVendorOfTheMonth": "vendor_of_the_month",
}


class ParseError(ValueError):
    """The page does not have the structure this parser expects."""


@dataclass(frozen=True)
class ModelLink:
    """A model category from the brand page. ``model`` is the canonical name, or None if it is not recognised."""

    slug: str
    model: str | None
    label: str


@dataclass(frozen=True)
class ListingVariant:
    variant_id: int
    url: str


@dataclass(frozen=True)
class ListingPage:
    total: int
    last_page: int
    variants: tuple[ListingVariant, ...]
    models: tuple[ModelLink, ...]
    refused: tuple[tuple[str, str], ...] = ()  # (url from the page, reason) for records whose URL is not allowed
    skipped: tuple[str, ...] = ()
    is_bot: bool | None = None  # how the site classified our User-Agent (listing pages only); worth watching


@dataclass(frozen=True)
class VariantPage:
    variant_id: int
    model: str
    in_stock: bool
    offers: tuple[RawOffer, ...]
    rejected: tuple[str, ...] = ()  # offers that failed validation, with the reason
    warnings: tuple[str, ...] = ()
    list_price_problems: tuple[str, ...] = ()  # implausible struck-through prices that were not used, with the reason


def check_url(url: str) -> str:
    """Name of the allowed form of ``url`` ("brand", "listing" or "variant"); raises ``urls.UrlNotAllowed``."""
    return urls.check(url, HOST, URL_FORMS)


def brand_url() -> str:
    return f"{BASE_URL}{CATALOG}/"


def listing_url(slug: str, page: int) -> str:
    url = f"{BASE_URL}{CATALOG}/{slug}/?pageNumber={page}"
    check_url(url)
    return url


def variant_url(path: str) -> str:
    """Fetch URL of a variant from the relative path a listing record gives, e.g.
    ``cep-telefonu/apple/iphone-13/apple-iphone-13-128-gb-mavi-813?merchantId=1``.

    The query is dropped (the default page lists every seller) and the trailing slash the site links with is added.
    """
    path = path.split("#", 1)[0].split("?", 1)[0].strip("/").removeprefix("satin-al/")
    url = f"{BASE_URL}/satin-al/{path}/"
    if check_url(url) != "variant":
        raise urls.UrlNotAllowed(f"{url}: not a variant page")
    return url


def offer_url(product_url: str, seller_id: int) -> str:
    """The link that opens this variant with ``seller_id`` as the selected seller."""
    url = f"{product_url}?merchantId={seller_id}"
    urls.check(url, HOST, OFFER_FORMS)
    return url


def variant_id_of(url: str) -> int | None:
    match = VARIANT_ID.search(url.split("?", 1)[0])
    return int(match.group(1)) if match else None


def parse_listing_page(html: str) -> ListingPage:
    """Parse the brand page or a model listing page (the brand page lists model groups, not variants)."""
    flight = _flight(html)
    holder = next(
        (d for d in flight.find_dicts("data") if isinstance(d["data"], dict) and "products" in d["data"]),
        None,
    )
    if holder is None:
        raise ParseError("no listing data (data.products) in the page")
    data = holder["data"]
    total, last_page = as_int(data.get("total")), as_int(data.get("lastPage"))
    if total is None or last_page is None or total < 0 or last_page < 0:
        raise ParseError("listing data without a valid total and lastPage")
    variants: list[ListingVariant] = []
    refused: list[tuple[str, str]] = []
    skipped: list[str] = []
    models = tuple(_model_links((data.get("aggregations") or {}).get("models") or [], skipped))
    for record in [] if data.get("groupedData") else data.get("products") or []:
        record_id = as_int(record.get("id")) if isinstance(record, dict) else None
        path = record.get("url") if isinstance(record, dict) else None
        if record_id is None or not isinstance(path, str):
            skipped.append("listing record without an id or url")
            continue
        try:
            url = variant_url(path)
        except urls.UrlNotAllowed as err:
            refused.append((path, str(err)))
            continue
        if variant_id_of(url) != record_id:
            skipped.append(f"listing record {record_id} links to variant {variant_id_of(url)}")
            continue
        variants.append(ListingVariant(record_id, url))
    is_bot = holder.get("isBot") if isinstance(holder.get("isBot"), bool) else None
    return ListingPage(total, last_page, tuple(variants), models, tuple(refused), tuple(skipped), is_bot)


def parse_variant_page(html: str, url: str, *, fetched_at: datetime, snapshot_date: date) -> VariantPage:
    """Offers of one variant page: the buybox seller plus every alternative seller.

    ``url`` is the page's final URL (after redirects); without its query it becomes the product URL of the offers.
    """
    flight = _flight(html)
    view = next(
        (d["view"] for d in flight.find_dicts("view") if isinstance(d["view"], dict) and "conditionGroup" in d["view"]),
        None,
    )
    if view is None:
        raise ParseError("no variant view (view.conditionGroup) in the page; not a variant page?")
    warnings: list[str] = []
    cart, price, stock = (_variant_block(flight, view, key, warnings) for key in ("cartInfo", "priceInfo", "stockInfo"))
    if cart is None or price is None or stock is None:
        raise ParseError("the variant view has no cartInfo, priceInfo or stockInfo")
    variant_id = as_int(cart.get("variantId"))
    if variant_id is None or variant_id != as_int(view.get("id")):
        raise ParseError(f"cartInfo.variantId {cart.get('variantId')!r} does not match the view id {view.get('id')!r}")
    model = canonical_model(view.get("titleBase"))
    if model is None:
        raise ParseError(f"unknown model {view.get('titleBase')!r}")
    if (requested := variant_id_of(url)) is not None and requested != variant_id:
        warnings.append(f"page for variant {requested} shows variant {variant_id}")

    in_stock = stock.get("inStock") is True and (as_int(stock.get("count")) or 0) > 0
    if not in_stock:
        return VariantPage(variant_id, model, False, (), (), tuple(warnings))

    product_url = url.split("?", 1)[0]
    if _form(product_url) != "variant":
        raise ParseError(f"{product_url} is not a variant URL")
    storage = storage_gb(view.get("storage"))
    if storage is None or not clean_text(view.get("color")) or not clean_text(view.get("condition")):
        raise ParseError(f"variant {variant_id} has no readable storage, colour or condition")
    build = _OfferBuilder(variant_id, model, storage, view, cart, product_url, fetched_at, snapshot_date)

    buybox_vendor = as_int(cart.get("vendorId"))
    vendor_info = view.get("vendorInfo") if isinstance(view.get("vendorInfo"), dict) else {}
    if as_int(vendor_info.get("id")) not in (None, buybox_vendor):
        warnings.append("vendorInfo.id differs from cartInfo.vendorId")
    buybox = _rename(vendor_info, VENDOR_FIELDS) | {
        "vendor_id": buybox_vendor,
        "stock": as_int(stock.get("count")),
        "low_stock": stock.get("lowStock"),
        "fast_delivery": view.get("isFastDelivery"),
        "shipping_days": view.get("shippingDays"),
        "strikethrough_price": price.get("strikethrough"),
        "battery_addon_price": _battery_addon(view, buybox_vendor),
    }
    build.add(
        buybox_vendor,
        price.get("main"),
        as_int(stock.get("count")),
        is_buybox=True,
        seller=buybox,
        list_price=price.get("strikethrough"),
    )

    alternatives = flight.first_value("alternativeSellersData")
    sellers = alternatives.get("sellers") if isinstance(alternatives, dict) else None
    if not isinstance(sellers, list):
        warnings.append("no alternativeSellersData.sellers list")
        sellers = []
    if isinstance(view.get("sellers"), list) and len(view["sellers"]) != len(sellers):
        warnings.append(f"view lists {len(view['sellers'])} alternative sellers, alternativeSellersData {len(sellers)}")
    seen = {buybox_vendor}
    for entry in sellers:
        vendor = as_int(entry.get("vendorId")) if isinstance(entry, dict) else None
        if vendor is None:
            warnings.append("alternative seller without a vendorId")
            continue
        if vendor in seen:
            warnings.append(f"seller {vendor} is listed twice")
            continue
        seen.add(vendor)
        seller = _rename(entry, SELLER_FIELDS)
        build.add(vendor, entry.get("price"), as_int(entry.get("stock")), is_buybox=False, seller=seller)

    if (label := SELLER_LABEL.search(html)) and int(label.group(1)) != len(sellers):
        warnings.append(f"seller label says {label.group(1)} alternatives, the data has {len(sellers)}")
    options = (view.get("conditionGroup") or {}).get("options") or []
    selected = [option for option in options if isinstance(option, dict) and option.get("isSelected")]
    if selected and as_decimal(selected[0].get("price")) != as_decimal(price.get("main")):
        warnings.append("selected condition price differs from priceInfo.main")
    return VariantPage(
        variant_id,
        model,
        True,
        tuple(build.offers),
        tuple(build.rejected),
        tuple(warnings),
        tuple(build.list_problems),
    )


@dataclass
class _OfferBuilder:
    """Turns the seller records of one variant page into validated offers; invalid ones become rejections."""

    variant_id: int
    model: str
    storage: int
    view: dict[str, Any]
    cart: dict[str, Any]
    product_url: str
    fetched_at: datetime
    snapshot_date: date
    offers: list[RawOffer] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    list_problems: list[str] = field(default_factory=list)

    def add(
        self,
        vendor: int | None,
        price: Any,
        stock: int | None,
        *,
        is_buybox: bool,
        seller: dict[str, Any],
        list_price: Any = None,
    ) -> None:
        key = f"getmobil:{self.variant_id}:{vendor}"
        try:
            if vendor is None or vendor < 1:
                raise ValueError("no vendor id")
            paid = as_decimal(price)
            if paid is None:
                raise ValueError("no price")
            listed, promo, problem = list_price_and_promo(paid, as_decimal(list_price))
            offer = RawOffer(
                platform=SOURCE,
                offer_key=key,
                snapshot_date=self.snapshot_date,
                fetched_at=self.fetched_at,
                model=self.model,
                storage_gb=self.storage,
                color=clean_text(self.view.get("color")),
                condition_raw=clean_text(self.view.get("condition")),
                sim_type="esim" if self.view.get("isEsimOnly") is True else "physical",
                sku=clean_text(self.cart.get("sku")) or None,
                seller_id=vendor,
                is_buybox=is_buybox,
                price_tl=paid,
                list_price_tl=listed,
                promo_pct=promo,
                stock_qty=stock,
                in_stock=(stock or 0) > 0,
                product_url=self.product_url,
                offer_url=offer_url(self.product_url, vendor),
                payload={"variant": self._variant(), "seller": seller},
                parser_version=PARSER_VERSION,
            )
        except (ValidationError, ValueError) as err:
            self.rejected.append(f"{key}: {error_summary(err)}")
            return
        self.offers.append(offer)
        if problem:
            self.list_problems.append(f"{key}: {problem}")

    def _variant(self) -> dict[str, Any]:
        view = self.view
        return {
            "variant_id": self.variant_id,
            "model_id": view.get("modelId"),
            "title": view.get("title"),
            "title_base": view.get("titleBase"),
            "condition": view.get("condition"),
            "storage": view.get("storage"),
            "color": view.get("color"),
            "esim_only": view.get("isEsimOnly"),
            "sku": self.cart.get("sku"),
        }


def crawl(ctx: CrawlContext) -> Iterator[RawOffer]:
    """Discover the in-scope variants model by model and yield the offers of each variant page.

    Working model by model keeps each listing read short (less drift) and lets a small request budget still produce
    complete offers for the first models.
    """
    brand = _fetch_listing(ctx, brand_url(), "brand")
    if brand is None:
        return
    fetched: set[int] = set()
    for link in _scope_models(ctx, brand.models):
        for variant in _discover_model(ctx, link).values():
            if variant.variant_id in fetched:
                continue
            if ctx.max_variants is not None and len(fetched) >= ctx.max_variants:
                ctx.stats.counters["variants_over_limit"] += 1
                continue
            fetched.add(variant.variant_id)
            yield from _variant_offers(ctx, variant)


def _scope_models(ctx: CrawlContext, models: tuple[ModelLink, ...]) -> list[ModelLink]:
    in_scope = [link for link in models if link.model and ctx.scope.includes(link.model)]
    ctx.stats.discovery.update(
        models_listed=len(models),
        models_in_scope=[link.model for link in in_scope],
        models_out_of_scope=[link.model for link in models if link.model and not ctx.scope.includes(link.model)],
        models_unknown=[link.label for link in models if not link.model],
        models=[],
    )
    for link in models:
        if not link.model:
            ctx.stats.warn(f"unknown model category {link.label!r} ({link.slug}) is skipped")
    if not models:
        ctx.stats.fail("brand", brand_url(), "no model categories on the brand page")
    return in_scope


def _discover_model(ctx: CrawlContext, link: ModelLink) -> dict[int, ListingVariant]:
    """Variant ids of one model. A listing that does not add up to ``data.total`` is read once more and the two
    readings are merged (each variant page is fetched anyway, so an extra id costs nothing in correctness)."""
    pages = _read_model_listing(ctx, link)
    found = _merge(pages)
    total = pages[1].total if pages[1] else None
    refetched = not _complete(pages, found)
    if refetched:
        log.info("%s: listing incomplete (%s of %s variants), reading it once more", link.slug, len(found), total)
        again = _read_model_listing(ctx, link)
        found |= _merge(again)
        total = again[1].total if again[1] else total
    record = {
        "model": link.model,
        "slug": link.slug,
        "total": total,
        "seen": len(found),
        "refetched": refetched,
        "missing": max(0, (total or 0) - len(found)),
    }
    ctx.stats.discovery["models"].append(record)
    ctx.stats.counters["variants_discovered"] += len(found)
    if total is None:
        ctx.stats.warn(f"{link.slug}: listing unavailable")
    elif record["missing"]:
        ctx.stats.warn(
            f"{link.slug}: data.total is {total} but {len(found)} distinct variants were listed after a re-fetch;"
            f" {record['missing']} missing"
        )
    return found


def _read_model_listing(ctx: CrawlContext, link: ModelLink) -> dict[int, ListingPage | None]:
    pages: dict[int, ListingPage | None] = {1: _fetch_listing(ctx, listing_url(link.slug, 1), "listing")}
    first = pages[1]
    for number in range(2, first.last_page + 1 if first else 0):
        pages[number] = _fetch_listing(ctx, listing_url(link.slug, number), "listing")
    return pages


def _merge(pages: dict[int, ListingPage | None]) -> dict[int, ListingVariant]:
    found: dict[int, ListingVariant] = {}
    for number in sorted(pages):
        page = pages[number]
        for variant in page.variants if page else ():
            found.setdefault(variant.variant_id, variant)
    return found


def _complete(pages: dict[int, ListingPage | None], found: dict[int, ListingVariant]) -> bool:
    """Every page was read, all pages report the same total, and the distinct ids add up to it."""
    read = [page for page in pages.values() if page is not None]
    if not read or len(read) != len(pages):
        return False
    return len({page.total for page in read}) == 1 and len(found) == read[0].total


def _fetch_listing(ctx: CrawlContext, url: str, kind: str) -> ListingPage | None:
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
        ctx.stats.refuse("variant", path, reason)
    for reason in page.skipped:
        ctx.stats.warn(f"{url}: {reason}")
    if page.is_bot is not None:
        ctx.stats.counters[f"listing_pages_is_bot_{str(page.is_bot).lower()}"] += 1
    return page


def _variant_offers(ctx: CrawlContext, variant: ListingVariant) -> Iterator[RawOffer]:
    response = ctx.get(variant.url, "variant")
    if response is None:
        return
    if response.status_code in (404, 410):
        # The listing index lags the product pages, so a listed variant can already be gone.
        ctx.stats.counters["variants_removed"] += 1
        ctx.stats.warn(f"variant {variant.variant_id} answered HTTP {response.status_code} (removed)")
        return
    if response.status_code != 200:
        ctx.stats.fail("variant", variant.url, f"HTTP {response.status_code}")
        return
    final_url = str(response.url)
    if _form(final_url.split("?", 1)[0]) != "variant":
        # A removed variant may be redirected to its model or brand page instead of answering 404.
        ctx.stats.counters["variants_redirected_away"] += 1
        return
    try:
        page = parse_variant_page(response.text, final_url, fetched_at=ctx.now(), snapshot_date=ctx.snapshot_date)
    except ParseError as err:
        ctx.stats.fail("variant", variant.url, str(err))
        return
    for warning in page.warnings:
        ctx.stats.warn(f"variant {page.variant_id}: {warning}")
    for reason in page.rejected:
        ctx.stats.counters["offers_rejected"] += 1
        ctx.stats.warn(f"rejected offer {reason}")
    for problem in page.list_price_problems:
        ctx.stats.counters["implausible_list_prices"] += 1
        ctx.stats.warn(f"implausible list price not used: {problem}")
    if not page.in_stock:
        ctx.stats.counters["variants_out_of_stock"] += 1
        return
    if not ctx.scope.includes(page.model):
        ctx.stats.counters["variants_out_of_scope"] += 1
        return
    ctx.stats.sellers_per_variant[len(page.offers)] += 1
    yield from page.offers


def _flight(html: str) -> Flight:
    try:
        return Flight.from_html(html)
    except FlightError as err:
        raise ParseError(str(err)) from err


def _variant_block(flight: Flight, view: dict[str, Any], key: str, warnings: list[str]) -> dict[str, Any] | None:
    """The view's own ``key`` (it belongs to the selected variant), else the first one on the page."""
    own = view.get(key) if isinstance(view.get(key), dict) else None
    found = flight.first_value(key)
    if own is not None and found is not None and own != found:
        warnings.append(f"view.{key} differs from the first {key} on the page")
    return own if own is not None else found


def _form(url: str) -> str | None:
    try:
        return check_url(url)
    except urls.UrlNotAllowed:
        return None


def _model_links(entries: list[Any], skipped: list[str]) -> Iterator[ModelLink]:
    """Model categories whose path is the one this adapter requests. Any other entry is reported in ``skipped``: on
    15.10.2026 Getmobil moved its catalogue and every model was dropped without a word."""
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        slug, label, path = entry.get("slug"), clean_text(entry.get("value")), entry.get("path")
        if not isinstance(slug, str) or not re.fullmatch(urls.SLUG, slug):
            skipped.append(f"model category with an unusable slug {slug!r}")
            continue
        if not isinstance(path, str) or path.rstrip("/") != f"{CATALOG.removeprefix('/satin-al')}/{slug}":
            skipped.append(f"model category {slug} has an unexpected path {path!r}")
            continue
        yield ModelLink(slug, canonical_model(slug) or canonical_model(label), label or slug)


def _battery_addon(view: dict[str, Any], vendor: int | None) -> Any:
    """Price of Getmobil's paid battery replacement for this offer, when the page offers one (only some models)."""
    for addon in view.get("relatedAddOns") or []:
        if not isinstance(addon, dict) or addon.get("addOnType") != "BATTERY_REPLACEMENT":
            continue
        if as_int(addon.get("vendorId")) in (None, vendor):
            return addon.get("calculatedPrice")
    return None


def _rename(record: dict[str, Any], fields: dict[str, str]) -> dict[str, Any]:
    return {new: record.get(old) for old, new in fields.items() if old in record}
