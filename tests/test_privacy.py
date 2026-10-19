"""Decisions K12 and K22: the parsed output carries no seller display names, vendor slugs, store links or legal
details (legal name, tax number and office, registration number, e-mail, address), only seller ids.

The fixtures contain such fields on purpose (with invented values), so these tests prove the parsers drop them.
"""

import json
from typing import Any

import pytest
from helpers import NOW, page
from pydantic import ValidationError

from price_radar.ingest.schemas import RawOffer
from price_radar.ingest.sources import easycep, getmobil, trendyol

FORBIDDEN_KEYS = (
    "name",
    "slug",
    "vendortitle",
    "storelink",
    "storeurl",
    "official",
    "tax",
    "registration",
    "email",
    "address",
    "city",
    "logo",
)
FORBIDDEN_TEXT = (
    "Example Store",
    "example-store-",
    "/magazalarimiz/",
    "Decoy Seller",
    "Elektronik Ticaret",  # legal names
    "@example.com",  # registered e-mail addresses
    "9999907001",  # a tax number
    "0999999999007001",  # a registration number
    "Vergi Dairesi",  # tax office
    "Deneme Sok.",  # address
    "Örnekşehir",  # city
)
TRENDYOL_PAGES = {
    "product_buybox_others.html": "/apple/yenilenmis-iphone-13-128-gb-mavi-ornek-urun-a-kalite-p-900000101",
    "product_condition_conflict.html": (
        "/apple/yenilenmis-iphone-14-pro-256-gb-derin-mor-ornek-urun-a-kalite-p-900000102"
    ),
    "product_misfiled.html": "/apple/iphone-12-mini-siyah-64gb-xyz1tu-a-ornek-outlet-c-kalite-p-900000105",
    "product_new_importer.html": "/apple/iphone-15-ornek-urun-128-gb-siyah-ithalatci-garantili-p-900000108",
}


def all_offers() -> list[RawOffer]:
    offers: list[RawOffer] = []
    base = "https://getmobil.com/satin-al/cep-telefonu/apple"
    for name, url in (
        ("variant_alternatives.html", f"{base}/iphone-13/apple-iphone-13-128-gb-mavi-900101/"),
        ("variant_esim.html", f"{base}/iphone-14-pro/apple-iphone-14-pro-256-gb-gumus-900103/"),
    ):
        result = getmobil.parse_variant_page(page(f"getmobil/{name}"), url, fetched_at=NOW, snapshot_date=NOW.date())
        offers.extend(result.offers)
    units = {
        unit.unit_id: unit
        for unit in map(easycep.parse_unit_page, (page("easycep/unit_flags.html"), page("easycep/unit_esim.html")))
    }
    for listing in ("listing_p1.html", "listing_p2.html"):
        for item in easycep.parse_listing_page(page(f"easycep/{listing}")).items:
            if easycep.item_model(item) != "iPhone 8":
                unit = units.get(item["id"])
                offers.append(easycep.build_offer(item, unit=unit, fetched_at=NOW, snapshot_date=NOW.date()))
    for name, path in TRENDYOL_PAGES.items():
        url = f"https://www.trendyol.com{path}"
        result = trendyol.parse_product_page(page(f"trendyol/{name}"), url, fetched_at=NOW, snapshot_date=NOW.date())
        offers.extend(result.offers)
    return offers


def keys(node: Any) -> list[str]:
    if isinstance(node, dict):
        return [str(key) for key in node] + [k for value in node.values() for k in keys(value)]
    if isinstance(node, list):
        return [k for value in node for k in keys(value)]
    return []


def test_the_fixtures_do_contain_names_slugs_and_store_links():
    html = page("getmobil/variant_alternatives.html")
    assert "displayName" in html and "vendorSlug" in html and "Example Store" in html and "/magazalarimiz/" in html


def test_the_trendyol_fixtures_do_contain_names_and_legal_details():
    html = page("trendyol/product_buybox_others.html")
    for key in ("officialName", "taxNumber", "registrationNumber", "taxOffice", "registeredEmailAddress", "address"):
        assert f'"{key}":' in html
    assert "Decoy Seller" in html and "Example Store Two" in html and "@example.com" in html


def test_parsed_offers_have_no_name_or_slug_fields_at_any_depth():
    offers = all_offers()
    assert len(offers) == 29
    assert {o.platform for o in offers} == {"getmobil", "easycep", "trendyol"}
    for offer in offers:
        record = json.loads(offer.model_dump_json())
        found = [key for key in keys(record) if any(word in key.lower().replace("_", "") for word in FORBIDDEN_KEYS)]
        assert found == [], offer.offer_key


def test_parsed_offers_contain_no_seller_names_slugs_or_store_links():
    output = "\n".join(offer.model_dump_json() for offer in all_offers())
    for text in FORBIDDEN_TEXT:
        assert text not in output


def test_sellers_are_identified_by_their_numeric_id_and_a_buy_link():
    offers = [o for o in all_offers() if o.platform == "getmobil"]
    assert all(isinstance(o.seller_id, int) for o in offers)
    assert all(o.offer_url.endswith(f"?merchantId={o.seller_id}") for o in offers)


def test_trendyol_merchants_are_identified_by_their_numeric_id_and_a_buy_link():
    offers = [o for o in all_offers() if o.platform == "trendyol"]
    assert all(isinstance(o.seller_id, int) for o in offers)
    assert all(o.offer_url == f"{o.product_url}?merchantId={o.seller_id}" for o in offers)
    assert all(
        set(o.payload["seller"])
        <= {
            "merchant_id",
            "corporate_invoice",
            "cash_on_delivery",
            "free_cargo",
            "rush_delivery",
            "seller_score",
            "badges",
        }
        for o in offers
    )


@pytest.mark.parametrize(
    "seller",
    [
        {"officialName": "Decoy Seller Elektronik Ticaret Ltd. Şti."},
        {"taxNumber": "9999907001"},
        {"taxOffice": "Örnek Vergi Dairesi"},
        {"registrationNumber": "0999999999007001"},
        {"mersisNo": "0999999999007001"},
        {"registeredEmailAddress": "decoy-7001@example.com"},
        {"address": "Örnek Mah. Deneme Sok. No:1"},
        {"cityName": "Örnekşehir"},
        {"store": "https://www.trendyol.com/magaza/decoy-seller-m-7001"},
    ],
)
def test_a_trendyol_payload_with_legal_details_or_a_store_link_is_rejected(seller):
    offer = next(o for o in all_offers() if o.platform == "trendyol")
    record = offer.model_dump() | {"payload": {"seller": seller}, "payload_hash": ""}
    with pytest.raises(ValidationError, match="seller names"):
        RawOffer(**record)
