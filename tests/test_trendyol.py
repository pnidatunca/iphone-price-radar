import json
from decimal import Decimal

import httpx
import pytest
from helpers import NOW, FakeSite, page

from price_radar.ingest.http_client import BudgetExhausted
from price_radar.ingest.schemas import RawOffer
from price_radar.ingest.sources import trendyol

BASE = "https://www.trendyol.com"
REFURBISHED = "/apple-yenilenmis-cep-telefonu-x-b101470-c109461"
NEW = "/iphone-ios-cep-telefonlari-x-c164462"
# Listed in the refurbished category.
P1 = "/apple/yenilenmis-iphone-13-128-gb-mavi-ornek-urun-a-kalite-p-900000101"
P2 = "/apple/yenilenmis-iphone-14-pro-256-gb-derin-mor-ornek-urun-a-kalite-p-900000102"  # also in the iPhone category
P3 = "/apple/yenilenmis-iphone-12-64-gb-beyaz-ornek-urun-b-kalite-p-900000103"
P6 = "/apple/yenilenmis-8-plus-64-gb-gumus-ornek-urun-b-kalite-p-900000106"
P7 = "/apple/yenilenmis-iphone-14-128-gb-mavi-ornek-urun-a-kalite-p-900000107"  # listed, but already gone (404)
# Listed in the iPhone category.
N1 = "/apple/iphone-15-ornek-urun-128-gb-siyah-p-900000104"
N2 = "/apple/iphone-15-ornek-urun-128-gb-siyah-ithalatci-garantili-p-900000108"
N3 = "/apple/iphone-14-128-gb-mavi-a-grade-ornek-urun-p-900000109"
N4 = "/apple/iphone-16-ornek-urun-256-gb-beyaz-p-900000110"
P5 = "/apple/iphone-12-mini-siyah-64gb-xyz1tu-a-ornek-outlet-c-kalite-p-900000105"  # refurbished, filed under iPhone
PAGES = {
    P1: "product_buybox_others.html",
    P2: "product_condition_conflict.html",
    P3: "product_out_of_stock.html",
    P6: "product_old_model.html",
    N1: "product_new_phone.html",
    N2: "product_new_importer.html",
    N3: "product_new_grade.html",
    N4: "product_new_no_warranty.html",
    P5: "product_misfiled.html",
}
# A few rules of Trendyol's robots.txt that concern the adapter's forms (all of them: tests/test_robots.py).
ROBOTS = (
    "User-agent: *\nDisallow: /*?sst=\nDisallow: /*&sst=\nDisallow: /*?q=\nDisallow: /sr?\n"
    "Disallow: /*/*-p-*/saticilar\nDisallow: /*-x-*-a*-v*?pi=\n"
)


def parse(path: str) -> trendyol.ProductPage:
    html = page(f"trendyol/{PAGES[path]}")
    return trendyol.parse_product_page(html, BASE + path, fetched_at=NOW, snapshot_date=NOW.date())


def parse_html(html: str, path: str) -> trendyol.ProductPage:
    return trendyol.parse_product_page(html, BASE + path, fetched_at=NOW, snapshot_date=NOW.date())


def state(name: str) -> dict:
    html = page(f"trendyol/{name}")
    block = trendyol.LISTING_STATE if "_listing_" in name else trendyol.PRODUCT_STATE
    return trendyol.window_state(html, block)


def html_with(block: str, value: object) -> str:
    return f'<html><body><script>window["{block}"]={json.dumps(value, ensure_ascii=False)}</script></body></html>'


def listing(total: int, page_index: int, cards: list | None = None, source: str = "refurbished_listing_p1.html") -> str:
    value = state(source)
    value["data"] |= {"total": total, "pageIndex": page_index}
    if cards is not None:
        value["data"]["products"] = cards
    return html_with(trendyol.LISTING_STATE, value)


def test_product_page_yields_the_buybox_and_every_in_stock_merchant():
    result = parse(P1)
    assert result.content_id == 900000101 and result.refurbished == ("category", "warranty", "title")
    assert result.kind == "refurbished"
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
    assert buybox.payload["product"]["classified_as"] == "refurbished"
    assert result.warnings == () and result.rejected == () and not result.seller_list_at_cap


def test_the_price_paid_is_the_discounted_price_not_the_original_or_a_coupon_price():
    # The buybox block also carries originalPrice 30499 and couponApplicablePrice 28999.
    buybox = parse(P1).offers[0]
    assert buybox.price_tl == Decimal("29449.05")
    offer = buybox.payload["offer"]
    assert (offer["selling_price"], offer["discounted_price"]) == (30999, 29449.05)
    assert offer["listing_id"] and offer["item_number"] == 800000101
    assert offer["basket_promotions"] == [{"promotion_id": 500001, "end_date": "2026-11-30T23:59:00", "applied": True}]


def test_out_of_stock_merchants_give_no_offer_even_when_cheaper():
    result = parse(P1)
    assert result.out_of_stock == 2
    assert {7004, 7005}.isdisjoint(o.seller_id for o in result.offers)  # inStock false; inStock without stockStatus 1


def test_an_out_of_stock_page_still_shows_prices_but_yields_no_offer():
    winner = state("product_out_of_stock.html")["product"]["merchantListing"]["winnerVariant"]
    assert winner["price"]["discountedPrice"]["value"] > 0
    result = parse(P3)
    assert (result.in_stock, result.offers, result.out_of_stock) == (False, (), 2)


def test_a_new_phone_with_apple_turkiye_warranty_gives_offers_of_every_merchant():
    result = parse(N1)
    assert (result.kind, result.refurbished, result.warranty) == ("new", (), "Apple Türkiye Garantili")
    assert [(o.offer_key, o.is_buybox, o.price_tl, o.promo_pct) for o in result.offers] == [
        ("trendyol:900000104:7010", True, Decimal("52999.00"), Decimal("0.00")),
        ("trendyol:900000104:7013", False, Decimal("53499.00"), Decimal("0.00")),
    ]
    offer = result.offers[0]
    assert (offer.model, offer.storage_gb, offer.color) == ("iPhone 15", 128, "Siyah")
    assert (offer.condition_raw, offer.condition_flags, offer.warranty_raw) == ("Sıfır", [], "Apple Türkiye Garantili")
    assert offer.payload["product"]["classified_as"] == "new" and offer.payload["product"]["refurbished_by"] == []
    assert result.warnings == ()


def test_a_new_phone_with_an_importer_warranty_and_thirteen_sellers():
    result = parse(N2)
    assert (result.kind, result.warranty) == ("new", "İthalatçı Garantili")
    assert len(result.offers) == 13 and len({o.offer_key for o in result.offers}) == 13
    assert {o.warranty_raw for o in result.offers} == {"İthalatçı Garantili"}
    assert result.seller_list_at_cap
    assert result.warnings == ("12 other merchants listed, as many as a page was seen to show: the list may be cut",)


def test_the_warranty_type_is_part_of_the_offer_hash():
    offer = parse(N1).offers[0]
    record = offer.model_dump() | {"warranty_raw": "İthalatçı Garantili", "payload_hash": ""}
    assert RawOffer(**record).payload_hash != offer.payload_hash


def test_a_new_phone_with_a_title_grade_is_a_conflict():
    result = parse(N3)
    assert result.kind == "new"  # "A Grade" is not the refurbished check's "A Kalite"
    [offer] = result.offers
    assert (offer.condition_raw, offer.condition_flags) == ("Sıfır", ["condition_conflict"])
    assert offer.payload["product"]["title_grade"] == "A"
    assert result.condition_conflict
    assert result.warnings == ("product classified as new has a grade (title 'A', Kozmetik Durum None)",)


def test_a_new_phone_with_a_kozmetik_durum_is_a_conflict():
    value = state("product_new_phone.html")
    value["product"]["attributes"].append(
        {"key": {"id": 2, "name": "Kozmetik Durum"}, "value": {"id": 7099, "name": "A seviye-Mükemmel"}}
    )
    result = parse_html(html_with(trendyol.PRODUCT_STATE, value), N1)
    assert result.kind == "new" and result.condition_conflict
    assert {tuple(o.condition_flags) for o in result.offers} == {("condition_conflict",)}
    assert {o.condition_raw for o in result.offers} == {"Sıfır"}


def test_a_new_phone_without_a_warranty_type_is_a_warning():
    result = parse(N4)
    assert (result.kind, result.warranty) == ("new", None)
    assert result.warnings == ("new product without a Garanti Tipi",)
    [offer] = result.offers
    assert (offer.warranty_raw, offer.price_tl, offer.promo_pct) == (None, Decimal("63999.00"), Decimal("1.54"))


def test_a_refurbished_phone_filed_under_ios_is_recognised_by_its_title():
    result = parse(P5)
    assert (result.kind, result.refurbished) == ("refurbished", ("title",))
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
    assert result.warnings == ()  # an empty Garanti Tipi is only a warning on new phones


def test_a_condition_conflict_is_flagged_and_the_title_names_the_colour():
    result = parse(P2)
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
    result = parse(P6)
    assert result.model == "iPhone 8 Plus"  # the title says "Yenilenmiş 8 Plus"; scope is applied by crawl()


def test_identity_comes_from_the_page_not_the_url():
    html = page("trendyol/product_buybox_others.html")
    result = parse_html(html, "/apple/baska-bir-urun-p-900000999?boutiqueId=7&merchantId=7002")
    assert result.warnings == ("page for product 900000999 shows product 900000101",)
    assert result.offers[0].offer_key == "trendyol:900000101:7001"
    assert result.offers[0].product_url == f"{BASE}/apple/baska-bir-urun-p-900000999"


def test_merchants_are_reduced_to_their_id_and_a_few_flags():
    offers = parse(P1).offers
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
    result = parse_html(html_with(trendyol.PRODUCT_STATE, value), P1)
    assert [o.seller_id for o in result.offers] == [7001, 7002, 7003]
    assert result.warnings == ("merchant 7002 is listed twice",)


def test_a_price_above_the_sticker_price_is_rejected_not_stored():
    value = state("product_buybox_others.html")
    value["product"]["merchantListing"]["otherMerchants"][0]["variants"][0]["price"]["discountedPrice"]["value"] = 40000
    result = parse_html(html_with(trendyol.PRODUCT_STATE, value), P1)
    assert [o.seller_id for o in result.offers] == [7001, 7003]
    assert len(result.rejected) == 1 and result.rejected[0].startswith("trendyol:900000101:7002:")


@pytest.mark.parametrize(
    ("html", "message"),
    [
        ("<html><body>Sayfa bulunamadı</body></html>", "no product data"),
        ('<script>window["__envoy-mobile__SHARED_PROPS"]={"product":{"id":1,</script>', "unreadable"),
        (html_with(trendyol.PRODUCT_STATE, {"product": {"id": 900000101, "name": "iPhone 13"}}), "merchantListing"),
        (page("trendyol/refurbished_listing_p1.html"), "no product data"),
    ],
    ids=["no state block", "unreadable block", "no merchant listing", "listing page"],
)
def test_a_product_page_without_the_expected_data_is_a_parse_error(html, message):
    with pytest.raises(trendyol.ParseError, match=message):
        parse_html(html, P1)


def test_a_product_page_without_any_price_is_a_parse_error():
    value = state("product_new_phone.html")
    listing_block = value["product"]["merchantListing"]
    for variant in [listing_block["winnerVariant"]] + [o["variants"][0] for o in listing_block["otherMerchants"]]:
        variant["price"] = {"currency": "TRY"}
    with pytest.raises(trendyol.ParseError, match="no prices"):
        parse_html(html_with(trendyol.PRODUCT_STATE, value), N1)


def test_a_listing_without_the_expected_data_is_a_parse_error():
    with pytest.raises(trendyol.ParseError, match="no listing data"):
        trendyol.parse_listing_page(page("trendyol/product_buybox_others.html"))
    with pytest.raises(trendyol.ParseError, match="data.total is 0"):
        trendyol.parse_listing_page(listing(0, 1))


def test_listing_page_gives_the_total_and_bare_product_urls():
    result = trendyol.parse_listing_page(page("trendyol/refurbished_listing_p1.html"))
    assert (result.total, result.page_index, result.records, len(result.organic)) == (38, 1, 36, 35)
    assert (len(result.cards), result.ads, result.not_apple) == (33, 1, 1)
    assert result.cards[0] == trendyol.ListingCard(
        900000101, BASE + P1, "Yenilenmiş iPhone 13 128 GB Mavi (Örnek Ürün) - A Kalite"
    )
    assert all("?" not in card.url for card in result.cards)  # boutiqueId and merchantId are dropped
    assert [path.split("?")[0] for path, _ in result.refused] == ["/apple/ornek-urun-p-900000192/yorumlar"]


def test_the_iphone_category_skips_sponsored_cards_and_other_brands():
    result = trendyol.parse_listing_page(page("trendyol/new_listing_p1.html"))
    assert (result.records, result.ads, result.not_apple, len(result.organic)) == (36, 12, 18, 24)
    assert [card.content_id for card in result.cards] == [
        900000102,
        900000104,
        900000108,
        900000109,
        900000110,
        900000105,
    ]


def site(**overrides) -> FakeSite:
    routes = {
        "/robots.txt": httpx.Response(200, text=ROBOTS),
        REFURBISHED: page("trendyol/refurbished_listing_p1.html"),
        f"{REFURBISHED}?pi=2": page("trendyol/refurbished_listing_p2.html"),
        NEW: page("trendyol/new_listing_p1.html"),
        f"{NEW}?pi=2": page("trendyol/new_listing_p2.html"),
    } | {path: page(f"trendyol/{name}") for path, name in PAGES.items()}
    return FakeSite(BASE, routes | overrides)


def test_crawl_reads_both_categories_and_collects_new_and_refurbished_offers():
    fake = site()
    ctx = fake.context()
    offers = list(trendyol.crawl(ctx))
    keys = [o.offer_key for o in offers]
    assert keys[:5] == [
        "trendyol:900000101:7001",
        "trendyol:900000101:7002",
        "trendyol:900000101:7003",
        "trendyol:900000102:7006",
        "trendyol:900000102:7007",
    ]
    assert keys[5:7] == ["trendyol:900000104:7010", "trendyol:900000104:7013"]
    assert keys[-3:] == ["trendyol:900000109:7015", "trendyol:900000110:7016", "trendyol:900000105:7011"]
    assert len(offers) == 23 and len(set(keys)) == 23
    assert sum(o.condition_raw == "Sıfır" for o in offers) == 17
    # Each category is read until N * 36 >= data.total; P2 is listed in both but requested once; the 31 cards of
    # older models are never requested.
    assert fake.requested == [
        "/robots.txt",
        REFURBISHED,
        f"{REFURBISHED}?pi=2",
        NEW,
        f"{NEW}?pi=2",
        P1,
        P2,
        P3,
        P6,
        P7,
        N1,
        N2,
        N3,
        N4,
        P5,
    ]
    stats = ctx.stats
    assert dict(stats.counters) == {
        "variants_discovered": 41,
        "refurbished_products_discovered": 35,
        "new_products_discovered": 6,
        "products_in_both_listings": 1,
        "cards_not_apple": 20,
        "cards_out_of_scope": 31,
        "refurbished_pages_requested": 5,
        "new_pages_requested": 5,
        "refurbished_products": 4,
        "new_products": 4,
        "refurbished_offers": 6,
        "new_offers": 17,
        "refurbished_in_new_listing": 1,
        "refurbished_outside_category": 1,
        "products_out_of_scope": 1,
        "products_out_of_stock": 1,
        "products_removed": 1,
        "offers_out_of_stock": 4,
        "condition_conflicts": 2,
        "seller_lists_at_cap": 1,
        "new_without_warranty_type": 1,
    }
    assert stats.observed == {
        "warranty_types_refurbished": {"Yenilenmiş Ürün (12 Ay Garanti)": 3},
        "warranty_types_new": {"Apple Türkiye Garantili": 2, "İthalatçı Garantili": 1},
    }
    assert stats.sellers_per_variant == {3: 1, 2: 2, 13: 1, 1: 3}
    assert stats.refusal_count == 1 and stats.failure_count == 0
    assert all(o.snapshot_date.isoformat() == "2026-10-12" for o in offers)


def test_discovery_is_reported_per_category():
    ctx = site().context(max_variants=0)
    list(trendyol.crawl(ctx))
    assert ctx.stats.discovery == {
        "categories": {
            "refurbished": {
                "listing_totals": [38],
                "listing_pages": 2,
                "listing_pages_failed": 0,
                "cards_seen": 38,
                "duplicate_cards": 1,
                "distinct_cards": 37,
                "missing": 1,
                "ads_skipped": 1,
                "not_apple_skipped": 1,
                "products_seen": 35,
                "listed_in_an_earlier_category": 0,
            },
            "new": {
                "listing_totals": [38],
                "listing_pages": 2,
                "listing_pages_failed": 0,
                "cards_seen": 26,
                "duplicate_cards": 0,
                "distinct_cards": 26,
                "missing": 12,  # the 12 sponsored cards of the full first page took the place of products
                "ads_skipped": 12,
                "not_apple_skipped": 19,
                "products_seen": 7,
                "listed_in_an_earlier_category": 1,
            },
        },
        "products_seen": 41,
    }
    warnings = [w for w in ctx.stats.warnings if "data.total" in w]
    assert [w.split(":")[0] for w in warnings] == ["refurbished listing", "new listing"]


def test_the_warnings_of_new_phones_reach_the_run_statistics():
    ctx = site().context()
    list(trendyol.crawl(ctx))
    warnings = ctx.stats.warnings
    assert "product 900000110: new product without a Garanti Tipi" in warnings
    assert any(w.startswith("product 900000109: product classified as new has a grade") for w in warnings)
    assert any(w.startswith("product 900000108: 12 other merchants listed") for w in warnings)


def test_the_listing_stops_at_a_page_without_products():
    fake = site(**{REFURBISHED: listing(100, 1), f"{REFURBISHED}?pi=2": listing(100, 2, cards=[])})
    ctx = fake.context(max_variants=0)
    list(trendyol.crawl(ctx))
    assert [path for path in fake.requested if path.startswith(REFURBISHED)] == [REFURBISHED, f"{REFURBISHED}?pi=2"]
    assert ctx.stats.discovery["categories"]["refurbished"]["listing_pages"] == 2


def test_a_failed_listing_page_does_not_end_discovery():
    fake = site(
        **{
            REFURBISHED: listing(100, 1),
            f"{REFURBISHED}?pi=2": httpx.Response(503),
            f"{REFURBISHED}?pi=3": listing(100, 3, source="refurbished_listing_p2.html"),
        }
    )
    ctx = fake.context(max_variants=0)
    list(trendyol.crawl(ctx))
    assert [path for path in fake.requested if path.startswith(REFURBISHED)] == [
        REFURBISHED,
        f"{REFURBISHED}?pi=2",
        f"{REFURBISHED}?pi=2",  # the retry
        f"{REFURBISHED}?pi=3",
    ]
    record = ctx.stats.discovery["categories"]["refurbished"]
    assert (record["listing_pages_failed"], record["products_seen"], ctx.stats.failure_count) == (1, 35, 1)


def test_a_failed_category_does_not_stop_the_other():
    fake = site(**{REFURBISHED: httpx.Response(404)})
    ctx = fake.context()
    offers = list(trendyol.crawl(ctx))
    assert fake.requested[:4] == ["/robots.txt", REFURBISHED, NEW, f"{NEW}?pi=2"]
    assert ctx.stats.failures[0]["error"] == "HTTP 404"
    assert {o.offer_key.split(":")[1] for o in offers} == {
        "900000102",
        "900000104",
        "900000108",
        "900000109",
        "900000110",
        "900000105",
    }
    assert ctx.stats.counters["new_products_discovered"] == 7  # P2 now counts as found in the iPhone category


def test_a_listing_that_changed_its_markup_is_reported():
    fake = site(**{NEW: "<html><body>yeni tasarım</body></html>"})
    ctx = fake.context(max_variants=0)
    list(trendyol.crawl(ctx))
    assert "no listing data" in ctx.stats.failures[0]["error"]
    assert ctx.stats.discovery["categories"]["new"]["products_seen"] == 0


def test_the_product_limit_caps_the_product_pages():
    fake = site()
    ctx = fake.context(max_variants=2)
    offers = list(trendyol.crawl(ctx))
    assert {o.offer_key.split(":")[1] for o in offers} == {"900000101", "900000102"}
    assert [path for path in fake.requested if "-p-" in path] == [P1, P2]
    assert ctx.stats.counters["products_over_limit"] == 8


def test_a_spent_budget_stops_the_crawl():
    fake = site()
    ctx = fake.context(max_requests=6)  # robots.txt, four listing pages, one product page
    with pytest.raises(BudgetExhausted):
        list(trendyol.crawl(ctx))
    assert fake.requested == ["/robots.txt", REFURBISHED, f"{REFURBISHED}?pi=2", NEW, f"{NEW}?pi=2", P1]
