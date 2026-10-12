from decimal import Decimal

import httpx
import pytest
from helpers import NOW, FakeSite, page

from price_radar.ingest.http_client import BudgetExhausted
from price_radar.ingest.sources import easycep

BASE = "https://easycep.com"
UNIT_1 = "/apple-iphone-13-128-gb-mavi-cep-telefonu-700001"
UNIT_2 = "/apple-iphone-14-128-gb-yildiz-isigi-cep-telefonu-700002"
UNIT_3 = "/apple-iphone-12-mini-64-gb-siyah-cep-telefonu-700003"


def listing_items(name: str) -> list[dict]:
    return list(easycep.parse_listing_page(page(f"easycep/{name}")).items)


def test_listing_page_gives_the_paging_and_the_items():
    listing = easycep.parse_listing_page(page("easycep/listing_p1.html"))
    assert (listing.total, listing.page, listing.page_count) == (4, 1, 2)
    assert [item["id"] for item in listing.items] == ["700001", "700002"]


def test_unit_page_gives_sku_flags_ean_and_warranty():
    unit = easycep.parse_unit_page(page("easycep/unit_flags.html"))
    assert unit.unit_id == "700001"
    assert unit.sku == easycep.SkuInfo("M-PIP13128GBZZPUEU-M", "M", "M", ("PU", "EU"))
    assert unit.ean == "0190000000012"  # a 12-digit UPC-A becomes the same 13-digit code as its EAN form
    assert unit.warranty == ("Yenilenmiş Telefon", "12 Ay Garanti", "14 Gün İçinde İade")
    assert (unit.is_new, unit.in_stock, unit.quantity) == (False, True, 2)


def test_unit_page_of_an_esim_only_device():
    unit = easycep.parse_unit_page(page("easycep/unit_esim.html"))
    assert unit.sku.flags == ("2ESIM",) and unit.sku.grade == "C"
    assert unit.ean == "0190000000029"


def test_unknown_sku_tokens_are_kept_as_raw_text():
    unit = easycep.parse_unit_page(page("easycep/unit_unknown_token.html"))
    assert unit.sku == easycep.SkuInfo("M-PIP12MINI64GBZİ3-O", "M", "O", ("3",))


def test_new_devices_are_recognised():
    unit = easycep.parse_unit_page(page("easycep/unit_new.html"))
    assert unit.is_new and unit.sku.prefix == "MS" and not unit.in_stock
    assert unit.warranty == ("Sıfır Telefon", "Distribütör Garantili", "24 Ay Garanti")


@pytest.mark.parametrize(
    ("sku", "flags"),
    [
        ("M-PIP13128GBZZ-M", ()),
        ("M-PIP13128GBZZEU-M", ("EU",)),
        ("M-PIP13PRO128GBZZZPUEU-C", ("PU", "EU")),
        ("M-PIPXSMAX256GBZZPUEUKU-O", ("PU", "EU", "KU")),
        ("M-PIP14PL512GBZZ2ESIM-M", ("2ESIM",)),
        ("M-PIP13PROMAX128GBZZZ3-I", ("3",)),
        ("M-PIP16PRO128GBZZTİT-O", ()),
        ("MS-PIP18PRO1TBZİ-M", ()),
    ],
)
def test_sku_flags_are_read_from_the_end(sku, flags):
    assert easycep.parse_sku(sku).flags == flags


def test_an_unexpected_sku_is_kept_without_guessing():
    assert easycep.parse_sku(" X-123 ") == easycep.SkuInfo("X-123", None, None, ())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("190000000012", "0190000000012"),
        ("0190000000029", "0190000000029"),
        (190000000012, "0190000000012"),
        ("00190000000012", "0190000000012"),
        ("12345678", "0000012345678"),
        ("1234", None),
        ("19000000001X", None),
        ("", None),
        (None, None),
    ],
)
def test_ean_is_normalised_to_13_digits(raw, expected):
    assert easycep.normalize_ean(raw) == expected


def test_offer_from_a_listing_item_with_unit_details():
    item = listing_items("listing_p1.html")[0]
    unit = easycep.parse_unit_page(page("easycep/unit_flags.html"))
    offer = easycep.build_offer(item, unit=unit, fetched_at=NOW, snapshot_date=NOW.date())
    assert offer.offer_key == "easycep:700001"
    assert (offer.model, offer.storage_gb, offer.color, offer.condition_raw) == ("iPhone 13", 128, "Mavi", "Mükemmel")
    assert (offer.price_tl, offer.stock_qty, offer.in_stock) == (Decimal("25490.00"), 2, True)
    assert (offer.seller_id, offer.is_buybox) == (None, True)
    assert offer.battery_raw == "%85-%100" and offer.warranty_raw == "12 Ay Garanti"
    assert (offer.sku, offer.condition_flags, offer.sim_type) == ("M-PIP13128GBZZPUEU-M", ["PU", "EU"], "physical")
    assert offer.product_url == offer.offer_url == f"{BASE}{UNIT_1}"
    assert offer.payload["unit"]["ean"] == "0190000000012"


def test_offer_without_unit_details_leaves_them_unknown():
    item = listing_items("listing_p1.html")[1]
    offer = easycep.build_offer(item, unit=None, fetched_at=NOW, snapshot_date=NOW.date())
    assert (offer.sku, offer.condition_flags, offer.sim_type, offer.warranty_raw) == (None, [], None, None)
    assert offer.payload["unit"] is None


def test_model_names_are_canonical():
    item = listing_items("listing_p2.html")[0]
    assert item["category"]["name"] == "iPhone 12 Mini"
    assert easycep.build_offer(item, unit=None, fetched_at=NOW, snapshot_date=NOW.date()).model == "iPhone 12 mini"


def test_esim_flag_sets_the_sim_type():
    item = listing_items("listing_p1.html")[1]
    unit = easycep.parse_unit_page(page("easycep/unit_esim.html"))
    assert easycep.build_offer(item, unit=unit, fetched_at=NOW, snapshot_date=NOW.date()).sim_type == "esim"


def site(**overrides) -> FakeSite:
    routes = {
        "/kategori/apple-2?page=1": page("easycep/listing_p1.html"),
        "/kategori/apple-2?page=2": page("easycep/listing_p2.html"),
        UNIT_1: page("easycep/unit_flags.html"),
        UNIT_2: page("easycep/unit_esim.html"),
        UNIT_3: page("easycep/unit_unknown_token.html"),
    }
    return FakeSite(BASE, routes | overrides)


def test_crawl_reads_the_listing_then_unit_pages_of_unseen_ids_only():
    fake = site()
    ctx = fake.context(known_ids=frozenset({"700002"}))
    offers = list(easycep.crawl(ctx))
    assert [o.offer_key for o in offers] == ["easycep:700001", "easycep:700002", "easycep:700003"]
    assert fake.requested == ["/robots.txt", "/kategori/apple-2?page=1", "/kategori/apple-2?page=2", UNIT_1, UNIT_3]
    assert [o.sku is not None for o in offers] == [True, False, True]
    assert ctx.stats.counters["items_out_of_scope"] == 1  # the iPhone 8 item (decision K21)
    assert ctx.stats.discovery["complete"] is True
    assert ctx.stats.sellers_per_variant == {1: 3}


def test_an_inconsistent_listing_is_read_once_more():
    fake = site(
        **{"/kategori/apple-2?page=2": [page("easycep/listing_p2_drift.html"), page("easycep/listing_p2.html")]}
    )
    ctx = fake.context(known_ids=frozenset({"700001", "700002", "700003"}))
    offers = list(easycep.crawl(ctx))
    assert fake.requested.count("/kategori/apple-2?page=1") == 2
    assert ctx.stats.discovery["refetched"] is True and ctx.stats.discovery["complete"] is True
    assert len(offers) == 3


def test_unit_page_limit_is_respected():
    fake = site()
    ctx = fake.context(max_variants=1)
    offers = list(easycep.crawl(ctx))
    assert len(offers) == 3
    assert [path for path in fake.requested if "cep-telefonu" in path] == [UNIT_1]
    assert ctx.stats.counters["units_over_limit"] == 2


def test_listing_offers_are_kept_when_unit_pages_fail():
    fake = site(**{UNIT_1: httpx.Response(503), UNIT_3: page("easycep/listing_p1.html")})
    ctx = fake.context()
    offers = list(easycep.crawl(ctx))
    assert len(offers) == 3 and all(o.sku is None for o in offers if o.offer_key != "easycep:700002")
    assert ctx.stats.failure_count == 2  # a 503 after its retry, and a unit URL that serves no product


def test_a_spent_budget_still_yields_the_listing_offers_then_stops():
    fake = site()
    ctx = fake.context(max_requests=4)  # robots.txt, two listing pages, one unit page
    offers = []
    with pytest.raises(BudgetExhausted):
        for offer in easycep.crawl(ctx):
            offers.append(offer)
    assert [o.offer_key for o in offers] == ["easycep:700001", "easycep:700002", "easycep:700003"]
    assert ctx.stats.counters["units_not_read_after_stop"] == 1
