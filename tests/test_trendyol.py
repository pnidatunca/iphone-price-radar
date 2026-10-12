import json
from decimal import Decimal

import httpx
import pytest
from helpers import NOW, FakeSite, page

from price_radar.ingest.http_client import BudgetExhausted
from price_radar.ingest.sources import trendyol

BASE = "https://www.trendyol.com"
CATEGORY = "/apple-yenilenmis-cep-telefonu-x-b101470-c109461"
P1 = "/apple/yenilenmis-iphone-13-128-gb-mavi-ornek-urun-a-kalite-p-900000101"
P2 = "/apple/yenilenmis-iphone-14-pro-256-gb-derin-mor-ornek-urun-a-kalite-p-900000102"
P3 = "/apple/yenilenmis-iphone-12-64-gb-beyaz-ornek-urun-b-kalite-p-900000103"
P4 = "/apple/iphone-15-ornek-urun-128-gb-siyah-p-900000104"
P5 = "/apple/iphone-12-mini-siyah-64gb-xyz1tu-a-ornek-outlet-c-kalite-p-900000105"
P6 = "/apple/yenilenmis-8-plus-64-gb-gumus-ornek-urun-b-kalite-p-900000106"
P7 = "/apple/yenilenmis-iphone-14-128-gb-mavi-ornek-urun-a-kalite-p-900000107"  # listed, but already gone (404)
# The parts of Trendyol's robots.txt that concern the adapter's forms.
ROBOTS = (
    "User-agent: *\nDisallow: /*?sst=\nDisallow: /*&sst=\nDisallow: /*?q=\nDisallow: /sr?\n"
    "Disallow: /*/*-p-*/saticilar\nDisallow: /*-x-*-a*-v*?pi=\n"
)


def parse(name: str, path: str = P1) -> trendyol.ProductPage:
    return trendyol.parse_product_page(page(f"trendyol/{name}"), BASE + path, fetched_at=NOW, snapshot_date=NOW.date())


def state(name: str) -> dict:
    html = page(f"trendyol/{name}")
    block = trendyol.LISTING_STATE if name.startswith("listing") else trendyol.PRODUCT_STATE
    return trendyol.window_state(html, block)


def html_with(block: str, value: object) -> str:
    return f'<html><body><script>window["{block}"]={json.dumps(value, ensure_ascii=False)}</script></body></html>'


def listing(total: int, page_index: int, cards: list | None = None, source: str = "listing_p1.html") -> str:
    value = state(source)
    value["data"] |= {"total": total, "pageIndex": page_index}
    if cards is not None:
        value["data"]["products"] = cards
    return html_with(trendyol.LISTING_STATE, value)


def test_product_page_yields_the_buybox_and_every_in_stock_merchant():
    result = parse("product_buybox_others.html")
    assert result.content_id == 900000101 and result.refurbished == ("category", "warranty", "title")
    assert [(o.seller_id, o.is_buybox, o.price_tl, o.list_price_tl, o.promo_pct) for o in result.offers] == [
        (7001, True, Decimal("29449.05"), Decimal("30999.00"), Decimal("5.00")),
        (7002, False, Decimal("29925.00"), Decimal("31500.00"), Decimal("5.00")),
        (7003, False, Decimal("30849.05"), Decimal("32999.00"), Decimal("6.52")),  # 5% plus a fixed 500 TL
    ]
    buybox = result.offers[0]
    assert buybox.offer_key == "trendyol:900000101:7001"
    assert (buybox.model, buybox.storage_gb, buybox.color, buybox.condition_raw) == (
        "iPhone 13",
        128,
        "Mavi",
        "A seviye-Mükemmel",
    )
    assert buybox.warranty_raw == "Yenilenmiş Ürün (12 Ay Garanti)" and buybox.condition_flags == []
    assert (buybox.sim_type, buybox.battery_raw, buybox.sku, buybox.stock_qty, buybox.in_stock) == (
        None,
        None,
        None,
        None,
        True,
    )
    assert buybox.product_url == BASE + P1
    assert {o.offer_url for o in result.offers} == {f"{BASE}{P1}?merchantId={m}" for m in (7001, 7002, 7003)}
    assert result.warnings == () and result.rejected == ()


def test_the_price_paid_is_the_discounted_price_not_the_original_or_a_coupon_price():
    # The buybox block also carries originalPrice 30499 and couponApplicablePrice 28999.
    buybox = parse("product_buybox_others.html").offers[0]
    assert buybox.price_tl == Decimal("29449.05")
    offer = buybox.payload["offer"]
    assert (offer["selling_price"], offer["discounted_price"]) == (30999, 29449.05)
    assert offer["listing_id"] and offer["item_number"] == 800000101
    assert offer["basket_promotions"] == [{"promotion_id": 500001, "end_date": "2026-11-30T23:59:00", "applied": True}]


def test_out_of_stock_merchants_give_no_offer_even_when_cheaper():
    result = parse("product_buybox_others.html")
    assert result.out_of_stock == 2
    assert {7004, 7005}.isdisjoint(o.seller_id for o in result.offers)  # inStock false; inStock without stockStatus 1


def test_an_out_of_stock_page_still_shows_prices_but_yields_no_offer():
    winner = state("product_out_of_stock.html")["product"]["merchantListing"]["winnerVariant"]
    assert winner["price"]["discountedPrice"]["value"] > 0
    result = parse("product_out_of_stock.html", P3)
    assert (result.in_stock, result.offers, result.out_of_stock) == (False, (), 2)


def test_a_new_phone_is_not_refurbished():
    result = parse("product_new_phone.html", P4)
    assert (result.model, result.refurbished, result.offers) == ("iPhone 15", (), ())


def test_a_refurbished_phone_filed_under_ios_is_recognised_by_its_title():
    result = parse("product_misfiled.html", P5)
    assert result.refurbished == ("title",)
    [offer] = result.offers
    assert (offer.model, offer.storage_gb, offer.color, offer.condition_raw) == (
        "iPhone 12 mini",
        64,  # no "Dahili Hafıza" attribute: from the title
        "Siyah",
        "Outlet C Kalite",  # no "Kozmetik Durum" attribute: the title grade
    )
    assert offer.warranty_raw is None and offer.condition_flags == []
    assert offer.payload["product"]["web_category_id"] == 164462
    assert offer.payload["product"]["title_grade"] == "Outlet C"


def test_a_condition_conflict_is_flagged_and_the_title_names_the_colour():
    result = parse("product_condition_conflict.html", P2)
    assert result.condition_conflict
    assert [(o.condition_raw, o.condition_flags, o.color) for o in result.offers] == [
        ("B seviye-Çok İyi", ["condition_conflict"], "Derin Mor")
    ] * 2
    product = result.offers[0].payload["product"]
    assert (product["condition_attribute"], product["title_grade"], product["color_raw"]) == (
        "B seviye-Çok İyi",
        "A",
        "Mor",
    )
    without_promotion = result.offers[1]
    assert without_promotion.price_tl == without_promotion.list_price_tl and without_promotion.promo_pct == 0


def test_the_model_attribute_wins_over_the_title():
    result = parse("product_old_model.html", P6)
    assert result.model == "iPhone 8 Plus"  # the title says "Yenilenmiş 8 Plus"; scope is applied by crawl()


def test_identity_comes_from_the_page_not_the_url():
    result = parse("product_buybox_others.html", "/apple/baska-bir-urun-p-900000999?boutiqueId=7&merchantId=7002")
    assert result.warnings == ("page for product 900000999 shows product 900000101",)
    assert result.offers[0].offer_key == "trendyol:900000101:7001"
    assert result.offers[0].product_url == f"{BASE}/apple/baska-bir-urun-p-900000999"


def test_merchants_are_reduced_to_their_id_and_a_few_flags():
    offers = parse("product_buybox_others.html").offers
    assert offers[0].payload["seller"] == {
        "merchant_id": 7001,
        "corporate_invoice": True,
        "cash_on_delivery": True,
        "seller_score": 9.2,
        "badges": ["MARKER"],
    }
    assert offers[1].payload["seller"] == {
        "merchant_id": 7002,
        "corporate_invoice": True,
        "free_cargo": True,
        "rush_delivery": False,
        "seller_score": 8.4,
        "badges": ["AUTHORIZED_SELLER"],
    }


def test_a_merchant_listed_twice_gives_one_offer():
    value = state("product_buybox_others.html")
    others = value["product"]["merchantListing"]["otherMerchants"]
    others.append(dict(others[0]))
    html = html_with(trendyol.PRODUCT_STATE, value)
    result = trendyol.parse_product_page(html, BASE + P1, fetched_at=NOW, snapshot_date=NOW.date())
    assert [o.seller_id for o in result.offers] == [7001, 7002, 7003]
    assert result.warnings == ("merchant 7002 is listed twice",)


def test_a_price_above_the_sticker_price_is_rejected_not_stored():
    value = state("product_buybox_others.html")
    value["product"]["merchantListing"]["otherMerchants"][0]["variants"][0]["price"]["discountedPrice"]["value"] = 40000
    html = html_with(trendyol.PRODUCT_STATE, value)
    result = trendyol.parse_product_page(html, BASE + P1, fetched_at=NOW, snapshot_date=NOW.date())
    assert [o.seller_id for o in result.offers] == [7001, 7003]
    assert len(result.rejected) == 1 and result.rejected[0].startswith("trendyol:900000101:7002:")


@pytest.mark.parametrize(
    ("html", "message"),
    [
        ("<html><body>Sayfa bulunamadı</body></html>", "no product data"),
        ('<script>window["__envoy-mobile__SHARED_PROPS"]={"product":{"id":1,</script>', "unreadable"),
        (html_with(trendyol.PRODUCT_STATE, {"product": {"id": 900000101, "name": "iPhone 13"}}), "merchantListing"),
        (page("trendyol/listing_p1.html"), "no product data"),
    ],
    ids=["no state block", "unreadable block", "no merchant listing", "listing page"],
)
def test_a_product_page_without_the_expected_data_is_a_parse_error(html, message):
    with pytest.raises(trendyol.ParseError, match=message):
        trendyol.parse_product_page(html, BASE + P1, fetched_at=NOW, snapshot_date=NOW.date())


def test_a_product_page_without_any_price_is_a_parse_error():
    value = state("product_buybox_others.html")
    listing_block = value["product"]["merchantListing"]
    for variant in [listing_block["winnerVariant"]] + [o["variants"][0] for o in listing_block["otherMerchants"]]:
        variant["price"] = {"currency": "TRY"}
    with pytest.raises(trendyol.ParseError, match="no prices"):
        trendyol.parse_product_page(
            html_with(trendyol.PRODUCT_STATE, value), BASE + P1, fetched_at=NOW, snapshot_date=NOW.date()
        )


def test_a_listing_without_the_expected_data_is_a_parse_error():
    with pytest.raises(trendyol.ParseError, match="no listing data"):
        trendyol.parse_listing_page(page("trendyol/product_buybox_others.html"))
    with pytest.raises(trendyol.ParseError, match="data.total is 0"):
        trendyol.parse_listing_page(listing(0, 1))


def test_listing_page_gives_the_total_and_bare_product_urls():
    result = trendyol.parse_listing_page(page("trendyol/listing_p1.html"))
    assert (result.total, result.page_index, result.records, len(result.cards), result.ads) == (38, 1, 36, 34, 1)
    assert result.cards[0] == trendyol.ListingCard(
        900000101, BASE + P1, "Yenilenmiş iPhone 13 128 GB Mavi (Örnek Ürün) - A Kalite"
    )
    assert all("?" not in card.url for card in result.cards)  # boutiqueId and merchantId are dropped
    assert [path.split("?")[0] for path, _ in result.refused] == [
        "/ornek-aksesuar/iphone-13-uyumlu-ornek-kilif-p-900000191"
    ]


def site(**overrides) -> FakeSite:
    routes = {
        "/robots.txt": httpx.Response(200, text=ROBOTS),
        CATEGORY: page("trendyol/listing_p1.html"),
        f"{CATEGORY}?pi=2": page("trendyol/listing_p2.html"),
        P1: page("trendyol/product_buybox_others.html"),
        P2: page("trendyol/product_condition_conflict.html"),
        P3: page("trendyol/product_out_of_stock.html"),
        P4: page("trendyol/product_new_phone.html"),
        P5: page("trendyol/product_misfiled.html"),
        P6: page("trendyol/product_old_model.html"),
    }
    return FakeSite(BASE, routes | overrides)


def test_crawl_discovers_the_products_and_collects_their_offers():
    fake = site()
    ctx = fake.context()
    offers = list(trendyol.crawl(ctx))
    assert [o.offer_key for o in offers] == [
        "trendyol:900000101:7001",
        "trendyol:900000101:7002",
        "trendyol:900000101:7003",
        "trendyol:900000102:7006",
        "trendyol:900000102:7007",
        "trendyol:900000105:7011",
    ]
    # Page 2 covers data.total (2 * 36 >= 38), so there is no page 3. The 29 cards of older models are never
    # requested, and the card that drifted from page 1 to page 2 is requested once.
    assert fake.requested == ["/robots.txt", CATEGORY, f"{CATEGORY}?pi=2", P1, P2, P3, P4, P5, P6, P7]
    stats = ctx.stats
    assert stats.discovery == {
        "listing_totals": [38],
        "listing_pages": 2,
        "listing_pages_failed": 0,
        "cards_seen": 37,
        "duplicate_cards": 1,
        "ads_skipped": 1,
        "products_seen": 36,
        "missing": 2,
    }
    assert any("data.total is 38 but 36 distinct products" in warning for warning in stats.warnings)
    assert dict(stats.counters) == {
        "variants_discovered": 36,
        "cards_out_of_scope": 29,
        "offers_out_of_stock": 4,
        "products_out_of_stock": 1,
        "products_not_refurbished": 1,
        "products_out_of_scope": 1,
        "products_refurbished_outside_category": 1,
        "condition_conflicts": 1,
        "products_removed": 1,
    }
    assert stats.sellers_per_variant == {3: 1, 2: 1, 1: 1}
    assert stats.refusal_count == 1  # the accessory card of another brand
    assert stats.failure_count == 0
    assert all(o.snapshot_date.isoformat() == "2026-10-12" for o in offers)


def test_the_listing_stops_at_a_page_without_products():
    fake = site(**{CATEGORY: listing(100, 1), f"{CATEGORY}?pi=2": listing(100, 2, cards=[])})
    ctx = fake.context(max_variants=0)
    list(trendyol.crawl(ctx))
    assert [path for path in fake.requested if path.startswith(CATEGORY)] == [CATEGORY, f"{CATEGORY}?pi=2"]
    assert ctx.stats.discovery["listing_pages"] == 2


def test_a_failed_listing_page_does_not_end_discovery():
    fake = site(
        **{
            CATEGORY: listing(100, 1),
            f"{CATEGORY}?pi=2": httpx.Response(503),
            f"{CATEGORY}?pi=3": listing(100, 3, source="listing_p2.html"),
        }
    )
    ctx = fake.context(max_variants=0)
    list(trendyol.crawl(ctx))
    assert [path for path in fake.requested if path.startswith(CATEGORY)] == [
        CATEGORY,
        f"{CATEGORY}?pi=2",
        f"{CATEGORY}?pi=2",  # the retry
        f"{CATEGORY}?pi=3",
    ]
    assert (ctx.stats.discovery["listing_pages_failed"], ctx.stats.failure_count) == (1, 1)
    assert ctx.stats.discovery["products_seen"] == 36


def test_without_the_first_listing_page_nothing_is_requested():
    fake = site(**{CATEGORY: httpx.Response(404)})
    ctx = fake.context()
    assert list(trendyol.crawl(ctx)) == []
    assert fake.requested == ["/robots.txt", CATEGORY]
    assert ctx.stats.failures[0]["error"] == "HTTP 404"


def test_a_listing_that_changed_its_markup_is_reported():
    fake = site(**{CATEGORY: "<html><body>yeni tasarım</body></html>"})
    ctx = fake.context()
    assert list(trendyol.crawl(ctx)) == []
    assert "no listing data" in ctx.stats.failures[0]["error"]


def test_the_product_limit_caps_the_product_pages():
    fake = site()
    ctx = fake.context(max_variants=2)
    offers = list(trendyol.crawl(ctx))
    assert {o.offer_key.split(":")[1] for o in offers} == {"900000101", "900000102"}
    assert [path for path in fake.requested if "-p-" in path] == [P1, P2]
    assert ctx.stats.counters["products_over_limit"] == 5


def test_a_spent_budget_stops_the_crawl():
    fake = site()
    ctx = fake.context(max_requests=4)  # robots.txt, two listing pages, one product page
    with pytest.raises(BudgetExhausted):
        list(trendyol.crawl(ctx))
    assert fake.requested == ["/robots.txt", CATEGORY, f"{CATEGORY}?pi=2", P1]
