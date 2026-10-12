from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from price_radar.ingest.normalize import as_decimal, as_int, canonical_model, storage_gb
from price_radar.ingest.schemas import RawOffer


def offer(**changes) -> dict:
    fields = {
        "platform": "getmobil",
        "offer_key": "getmobil:900101:7001",
        "snapshot_date": date(2026, 10, 12),
        "fetched_at": datetime(2026, 10, 12, 0, 20, tzinfo=UTC),
        "model": "iPhone 13",
        "storage_gb": 128,
        "color": "Mavi",
        "condition_raw": "Mükemmel",
        "sim_type": "physical",
        "seller_id": 7001,
        "is_buybox": True,
        "price_tl": "24990",
        "stock_qty": 3,
        "in_stock": True,
        "product_url": "https://getmobil.com/satin-al/x/apple-iphone-13-128-gb-mavi-900101/",
        "offer_url": "https://getmobil.com/satin-al/x/apple-iphone-13-128-gb-mavi-900101/?merchantId=7001",
        "payload": {"seller": {"vendor_id": 7001, "score": 9}},
        "parser_version": "getmobil-1",
    }
    return fields | changes


def test_a_valid_offer_gets_its_hash_and_survives_a_json_round_trip():
    record = RawOffer(**offer())
    assert record.price_tl == Decimal("24990.00")
    assert len(record.payload_hash) == 64
    assert RawOffer.model_validate_json(record.model_dump_json()) == record


@pytest.mark.parametrize("price", ["0", "-5", "1000001", None])
def test_price_must_be_positive_and_plausible(price):
    with pytest.raises(ValidationError):
        RawOffer(**offer(price_tl=price))


@pytest.mark.parametrize("storage", [16, 32, 64, 128, 256, 512, 1024, 2048])
def test_every_iphone_storage_size_is_accepted(storage):
    assert RawOffer(**offer(storage_gb=storage)).storage_gb == storage


@pytest.mark.parametrize("storage", [0, 100, 1000])
def test_unknown_storage_sizes_are_rejected(storage):
    with pytest.raises(ValidationError):
        RawOffer(**offer(storage_gb=storage))


@pytest.mark.parametrize("field", ["model", "color", "condition_raw"])
def test_descriptive_fields_must_not_be_blank(field):
    with pytest.raises(ValidationError):
        RawOffer(**offer(**{field: "  "}))


@pytest.mark.parametrize(
    "changes",
    [
        {"offer_key": "easycep:900101"},
        {"product_url": "http://getmobil.com/x/"},
        {"offer_url": "https://easycep.com/x"},
        {"seller_id": 0},
        {"stock_qty": -1},
        {"sim_type": "dual"},
    ],
)
def test_inconsistent_offers_are_rejected(changes):
    with pytest.raises(ValidationError):
        RawOffer(**offer(**changes))


@pytest.mark.parametrize(
    "payload",
    [
        {"seller": {"displayName": "Example Store"}},
        {"seller": {"vendorSlug": "example-store"}},
        {"sellers": [{"vendorTitle": "Example Store Ltd"}]},
        {"seller": {"store_url": "x"}},
        {"links": ["https://getmobil.com/magazalarimiz/example-store-7001/"]},
    ],
)
def test_payload_with_seller_names_slugs_or_store_links_is_rejected(payload):
    with pytest.raises(ValidationError, match="seller names"):
        RawOffer(**offer(payload=payload))


def test_the_record_has_no_field_for_names_or_slugs():
    assert not [name for name in RawOffer.model_fields if "name" in name or "slug" in name]


def test_hash_follows_the_offer_but_ignores_volatile_payload_details():
    base = RawOffer(**offer())
    assert RawOffer(**offer(payload={"seller": {"vendor_id": 7001, "score": 7}})).payload_hash == base.payload_hash
    assert RawOffer(**offer(price_tl="24890")).payload_hash != base.payload_hash
    assert RawOffer(**offer(stock_qty=2)).payload_hash != base.payload_hash


def test_a_stale_hash_is_rejected():
    record = RawOffer(**offer())
    with pytest.raises(ValidationError, match="payload_hash"):
        RawOffer(**(record.model_dump() | {"price_tl": Decimal("1")}))


def test_offers_are_immutable():
    record = RawOffer(**offer())
    with pytest.raises(ValidationError):
        record.price_tl = Decimal("1")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Yenilenmiş Apple iPhone 13 Pro Max", "iPhone 13 Pro Max"),
        ("iphone-13-pro-max", "iPhone 13 Pro Max"),
        ("iPhone 12 Mini", "iPhone 12 mini"),
        ("iPhone 13 mini", "iPhone 13 mini"),
        ("iPhone 16E", "iPhone 16e"),
        ("iphone-16e", "iPhone 16e"),
        ("iphone se (2nd generation)", "iPhone SE (2020)"),
        ("iphone se (3rd generation)", "iPhone SE (2022)"),
        ("Yenilenmiş Apple iPhone SE 2020", "iPhone SE (2020)"),
        ("iphone-se-2022", "iPhone SE (2022)"),
        ("iPhone X", "iPhone X"),
        ("iPhone XR", "iPhone XR"),
        ("iPhone XS", "iPhone XS"),
        ("iphone-xs-max", "iPhone XS Max"),
        ("iPhone Air", "iPhone Air"),
        ("iPhone 6s Plus", "iPhone 6s Plus"),
        ("iPhone 8", "iPhone 8"),
        ("iPhone 17 Pro", "iPhone 17 Pro"),
    ],
)
def test_model_names_are_canonicalised(raw, expected):
    assert canonical_model(raw) == expected


@pytest.mark.parametrize(
    "raw", [None, "", "Galaxy S24", "iPhone SE", "iPhone XR Max", "iphone-fold", "iPhone 13 Kılıf"]
)
def test_unknown_models_are_not_guessed(raw):
    assert canonical_model(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("128 GB", 128), ("64GB", 64), ("1 TB", 1024), ("2 TB", 2048), ("0,5 TB", 512), ("E-Sim", None), (None, None)],
)
def test_storage_sizes_are_read_in_gigabytes(raw, expected):
    assert storage_gb(raw) == expected


def test_numbers_from_page_data():
    assert as_int("12") == 12 and as_int(True) is None and as_int("1.5") is None
    assert as_decimal("106999.00") == Decimal("106999.00") and as_decimal(36749.15) == Decimal("36749.15")
    assert as_decimal("NaN") is None and as_decimal(None) is None and as_decimal("abc") is None
