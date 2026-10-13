from decimal import Decimal

import httpx
import pytest
from helpers import NOW, FakeSite, page

from price_radar.ingest.http_client import BudgetExhausted
from price_radar.ingest.sources import getmobil

BASE = "https://getmobil.com"
CATALOG = "/satin-al/cep-telefonu/iphone-ios-telefonlar/apple"
VARIANT_URL = f"{BASE}{CATALOG}/iphone-13/apple-iphone-13-128-gb-mavi-900101/"


def parse(name: str, url: str = VARIANT_URL) -> getmobil.VariantPage:
    return getmobil.parse_variant_page(page(f"getmobil/{name}"), url, fetched_at=NOW, snapshot_date=NOW.date())


def test_variant_page_yields_the_buybox_and_every_alternative_seller():
    result = parse("variant_alternatives.html")
    assert result.in_stock and result.variant_id == 900101 and not result.warnings
    assert [(o.seller_id, o.is_buybox, o.price_tl, o.stock_qty) for o in result.offers] == [
        (7001, True, Decimal("24990.00"), 3),
        (7002, False, Decimal("24490.00"), 1),
        (7003, False, Decimal("25990.00"), 2),
        (7004, False, Decimal("26590.00"), 1),
        (7005, False, Decimal("27090.00"), 4),
        (7006, False, Decimal("27990.00"), 1),
    ]
    buybox = result.offers[0]
    assert buybox.offer_key == "getmobil:900101:7001"
    assert (buybox.model, buybox.storage_gb, buybox.color, buybox.condition_raw) == (
        "iPhone 13",
        128,
        "Mavi",
        "Mükemmel",
    )
    assert buybox.sim_type == "physical" and buybox.sku == "9999010119001"
    assert buybox.battery_raw is None and buybox.warranty_raw is None and buybox.condition_flags == []
    assert buybox.product_url == VARIANT_URL
    assert buybox.offer_url == f"{VARIANT_URL}?merchantId=7001"
    assert {o.offer_url for o in result.offers[1:]} == {f"{VARIANT_URL}?merchantId={v}" for v in range(7002, 7007)}


def test_the_buybox_is_not_assumed_to_be_the_cheapest_offer():
    offers = parse("variant_alternatives.html").offers
    assert min(offers, key=lambda o: o.price_tl).seller_id == 7002


def test_json_ld_offers_are_never_used():
    # The fixture's JSON-LD names seller 7999 at 1 TL, like the real JSON-LD that lists only cheap alternatives.
    offers = parse("variant_alternatives.html").offers
    assert 7999 not in {o.seller_id for o in offers}
    assert all(o.price_tl > 1000 for o in offers)


def test_out_of_stock_page_yields_no_offer_despite_its_price():
    result = parse("variant_out_of_stock.html", VARIANT_URL.replace("900101", "900102"))
    assert result.variant_id == 900102
    assert not result.in_stock
    assert result.offers == ()


def test_esim_variant_is_marked():
    url = f"{BASE}{CATALOG}/iphone-14-pro/apple-iphone-14-pro-256-gb-gumus-900103/"
    result = parse("variant_esim.html", url)
    assert [(o.offer_key, o.sim_type, o.model, o.storage_gb) for o in result.offers] == [
        ("getmobil:900103:7010", "esim", "iPhone 14 Pro", 256)
    ]


def test_identity_comes_from_the_page_data_not_the_url():
    # A stale slug naming another storage and colour does not change what the page is.
    stale = f"{BASE}{CATALOG}/iphone-13/apple-iphone-13-512-gb-siyah-900101/"
    result = parse("variant_alternatives.html", stale)
    assert {(o.storage_gb, o.color) for o in result.offers} == {(128, "Mavi")}
    other_id = f"{BASE}{CATALOG}/iphone-13/apple-iphone-13-128-gb-mavi-900555/"
    result = parse("variant_alternatives.html", other_id)
    assert result.offers[0].offer_key == "getmobil:900101:7001"
    assert result.warnings == ("page for variant 900555 shows variant 900101",)


def test_offer_urls_keep_only_the_seller_parameter():
    result = parse("variant_alternatives.html", f"{VARIANT_URL}?merchantId=7004")
    assert result.offers[0].product_url == VARIANT_URL
    assert result.offers[0].offer_url == f"{VARIANT_URL}?merchantId=7001"


def test_a_page_that_is_not_a_variant_page_is_a_parse_error():
    with pytest.raises(getmobil.ParseError):
        parse("listing_iphone-13_p1.html")
    with pytest.raises(getmobil.ParseError):
        getmobil.parse_variant_page("<html>404</html>", VARIANT_URL, fetched_at=NOW, snapshot_date=NOW.date())


def test_brand_page_lists_the_model_categories():
    listing = getmobil.parse_listing_page(page("getmobil/brand.html"))
    assert [(m.slug, m.model) for m in listing.models] == [
        ("iphone-13", "iPhone 13"),
        ("iphone-14-pro", "iPhone 14 Pro"),
        ("iphone-8", "iPhone 8"),
        ("iphone-fold", None),
    ]
    assert listing.variants == ()  # the brand page lists model groups, not variants
    assert listing.is_bot is False


def test_model_listing_gives_variant_ids_and_fetch_urls():
    listing = getmobil.parse_listing_page(page("getmobil/listing_iphone-13_p2.html"))
    assert (listing.total, listing.last_page) == (3, 2)
    assert listing.variants == (
        getmobil.ListingVariant(900102, f"{BASE}{CATALOG}/iphone-13/apple-iphone-13-128-gb-mavi-900102/"),
        getmobil.ListingVariant(900104, f"{BASE}{CATALOG}/iphone-13/apple-iphone-13-128-gb-siyah-900104/"),
    )


def test_listing_records_with_urls_outside_the_allowlist_are_refused():
    listing = getmobil.parse_listing_page(page("getmobil/listing_iphone-14-pro_p1.html"))
    assert [v.variant_id for v in listing.variants] == [900103]
    assert [path for path, _ in listing.refused] == ["cep-telefonu/aksesuar/kilif/ornek-kilif-900199/?merchantId=7011"]


def site(**overrides) -> FakeSite:
    routes = {
        f"{CATALOG}/": page("getmobil/brand.html"),
        f"{CATALOG}/iphone-13/?pageNumber=1": page("getmobil/listing_iphone-13_p1.html"),
        f"{CATALOG}/iphone-13/?pageNumber=2": page("getmobil/listing_iphone-13_p2.html"),
        f"{CATALOG}/iphone-14-pro/?pageNumber=1": page("getmobil/listing_iphone-14-pro_p1.html"),
        f"{CATALOG}/iphone-13/apple-iphone-13-128-gb-mavi-900101/": page("getmobil/variant_alternatives.html"),
        f"{CATALOG}/iphone-13/apple-iphone-13-128-gb-mavi-900102/": page("getmobil/variant_out_of_stock.html"),
        f"{CATALOG}/iphone-14-pro/apple-iphone-14-pro-256-gb-gumus-900103/": page("getmobil/variant_esim.html"),
        # 900104 is listed but already gone: the site answers 404.
    }
    return FakeSite(BASE, routes | overrides)


def test_crawl_discovers_in_scope_variants_and_collects_their_offers():
    fake = site()
    ctx = fake.context()
    offers = list(getmobil.crawl(ctx))
    assert [o.offer_key for o in offers] == [f"getmobil:900101:{v}" for v in range(7001, 7007)] + [
        "getmobil:900103:7010"
    ]
    assert fake.requested == [
        "/robots.txt",
        f"{CATALOG}/",
        f"{CATALOG}/iphone-13/?pageNumber=1",
        f"{CATALOG}/iphone-13/?pageNumber=2",
        f"{CATALOG}/iphone-13/apple-iphone-13-128-gb-mavi-900101/",
        f"{CATALOG}/iphone-13/apple-iphone-13-128-gb-mavi-900102/",
        f"{CATALOG}/iphone-13/apple-iphone-13-128-gb-siyah-900104/",
        f"{CATALOG}/iphone-14-pro/?pageNumber=1",
        f"{CATALOG}/iphone-14-pro/apple-iphone-14-pro-256-gb-gumus-900103/",
    ]
    stats = ctx.stats
    assert stats.discovery["models_in_scope"] == ["iPhone 13", "iPhone 14 Pro"]
    assert stats.discovery["models_out_of_scope"] == ["iPhone 8"]  # never requested (decision K21)
    assert stats.discovery["models_unknown"] == ["iPhone Fold"]
    assert stats.sellers_per_variant == {6: 1, 1: 1}
    assert stats.counters["variants_out_of_stock"] == 1
    assert stats.counters["variants_removed"] == 1
    assert stats.refusal_count == 1  # the accessory record of the iPhone 14 Pro listing
    assert all(o.snapshot_date.isoformat() == "2026-10-12" for o in offers)


def test_duplicate_listing_records_are_fetched_once():
    fake = site()
    list(getmobil.crawl(fake.context()))
    assert fake.requested.count(f"{CATALOG}/iphone-13/apple-iphone-13-128-gb-mavi-900102/") == 1


def test_an_incomplete_listing_is_read_once_more():
    drifted = page("getmobil/listing_iphone-13_p2_drift.html")
    fake = site(**{f"{CATALOG}/iphone-13/?pageNumber=2": [drifted, page("getmobil/listing_iphone-13_p2.html")]})
    ctx = fake.context()
    list(getmobil.crawl(ctx))
    assert fake.requested.count(f"{CATALOG}/iphone-13/?pageNumber=1") == 2
    record = ctx.stats.discovery["models"][0]
    assert record == {"model": "iPhone 13", "slug": "iphone-13", "total": 3, "seen": 3, "refetched": True, "missing": 0}


def test_what_is_still_missing_after_the_re_fetch_is_reported():
    fake = site(**{f"{CATALOG}/iphone-13/?pageNumber=2": page("getmobil/listing_iphone-13_p2_drift.html")})
    ctx = fake.context()
    list(getmobil.crawl(ctx))
    record = ctx.stats.discovery["models"][0]
    assert (record["refetched"], record["seen"], record["missing"]) == (True, 2, 1)
    assert any("1 missing" in warning for warning in ctx.stats.warnings)


def test_a_failed_listing_page_triggers_the_re_fetch():
    p2 = page("getmobil/listing_iphone-13_p2.html")
    fake = site(**{f"{CATALOG}/iphone-13/?pageNumber=2": [httpx.Response(500), httpx.Response(500), p2]})
    ctx = fake.context()
    offers = list(getmobil.crawl(ctx))
    assert ctx.stats.discovery["models"][0]["missing"] == 0
    assert ctx.stats.failure_count == 1
    assert len(offers) == 7


def test_variant_limit_caps_the_variant_pages():
    fake = site()
    ctx = fake.context(max_variants=1)
    offers = list(getmobil.crawl(ctx))
    assert {o.offer_key.split(":")[1] for o in offers} == {"900101"}
    assert ctx.stats.counters["variants_over_limit"] == 3


def test_a_spent_budget_stops_the_crawl():
    fake = site()
    ctx = fake.context(max_requests=5)
    with pytest.raises(BudgetExhausted):
        list(getmobil.crawl(ctx))
    assert len(fake.requested) == 5


def test_a_buybox_with_a_struck_through_price_gets_it_as_its_list_price():
    url = f"{BASE}{CATALOG}/iphone-15-pro-max/apple-iphone-15-pro-max-256-gb-siyah-900105/"
    result = parse("variant_strikethrough.html", url)
    assert [(o.seller_id, o.is_buybox, o.price_tl, o.list_price_tl, o.promo_pct) for o in result.offers] == [
        (7030, True, Decimal("51990.00"), Decimal("55990.00"), Decimal("7.14")),
        # Alternative sellers show no earlier price of their own: the list price is the price.
        (7031, False, Decimal("52490.00"), Decimal("52490.00"), Decimal("0.00")),
    ]
    assert result.list_price_problems == ()


def test_without_a_struck_through_price_the_list_price_is_the_price():
    offers = parse("variant_alternatives.html").offers
    assert all((o.list_price_tl, o.promo_pct) == (o.price_tl, 0) for o in offers)
    assert offers[0].payload["seller"]["strikethrough_price"] is None


def test_a_struck_through_price_below_the_price_is_not_used():
    url = f"{BASE}{CATALOG}/iphone-15-pro-max/apple-iphone-15-pro-max-256-gb-mavi-900106/"
    result = parse("variant_strikethrough_below.html", url)
    buybox = result.offers[0]
    assert (buybox.seller_id, buybox.price_tl, buybox.list_price_tl, buybox.promo_pct) == (
        7032,
        Decimal("51990.00"),
        Decimal("51990.00"),
        0,
    )
    assert buybox.payload["seller"]["strikethrough_price"] == 5199  # kept raw in the payload
    assert result.list_price_problems == ("getmobil:900106:7032: list price 5199 is below the price paid 51990",)


def test_an_implausible_struck_through_price_is_counted_in_the_run():
    fake = site(
        **{
            f"{CATALOG}/iphone-13/apple-iphone-13-128-gb-mavi-900101/": page(
                "getmobil/variant_strikethrough_below.html"
            )
        }
    )
    ctx = fake.context()
    list(getmobil.crawl(ctx))
    assert ctx.stats.counters["implausible_list_prices"] == 1
    assert any(w.startswith("implausible list price not used: getmobil:900106:7032") for w in ctx.stats.warnings)
