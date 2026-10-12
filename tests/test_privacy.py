"""Decision K12: the parsed output carries no seller display names, vendor slugs or store links, only seller ids.

The fixtures contain such fields on purpose (with invented values), so these tests prove the parsers drop them.
"""

import json
from typing import Any

from helpers import NOW, page

from price_radar.ingest.schemas import RawOffer
from price_radar.ingest.sources import easycep, getmobil

FORBIDDEN_KEYS = ("name", "slug", "vendortitle", "storelink", "storeurl")
FORBIDDEN_TEXT = ("Example Store", "example-store-", "/magazalarimiz/", "Decoy Seller")


def all_offers() -> list[RawOffer]:
    offers: list[RawOffer] = []
    base = "https://getmobil.com/satin-al/cep-telefonu/iphone-ios-telefonlar/apple"
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


def test_parsed_offers_have_no_name_or_slug_fields_at_any_depth():
    offers = all_offers()
    assert len(offers) == 10
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
