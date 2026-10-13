import pytest

from price_radar.ingest.sources import easycep, getmobil, trendyol
from price_radar.ingest.urls import UrlNotAllowed

CATALOG = "https://getmobil.com/satin-al/cep-telefonu/iphone-ios-telefonlar/apple"
VARIANT = f"{CATALOG}/iphone-13/apple-iphone-13-128-gb-mavi-900101/"


@pytest.mark.parametrize(
    ("url", "form"),
    [
        (f"{CATALOG}/", "brand"),
        (f"{CATALOG}/iphone-13/?pageNumber=1", "listing"),
        (f"{CATALOG}/iphone-13/?pageNumber=12", "listing"),
        (f"{CATALOG}/iphone-13/", "listing"),
        (VARIANT, "variant"),
    ],
)
def test_getmobil_allows_its_page_forms(url, form):
    assert getmobil.check_url(url) == form


@pytest.mark.parametrize(
    "url",
    [
        f"{CATALOG}/iphone-13/?sortStrategy=price",
        f"{CATALOG}/iphone-13/?pageNumber=2&sortStrategy=price",
        f"{CATALOG}/iphone-13/?pageNumber=2&condition=1",
        f"{CATALOG}/iphone-13/?149=1",
        f"{CATALOG}/iphone-13/?minPrice=100",
        f"{CATALOG}/iphone-13/?pageNumber=0",
        f"{CATALOG}/iphone-13/?pageNumber=two",
        f"{CATALOG}/iphone-13/?pageNumber=2%26orderby%3D1",
        f"{CATALOG}/iphone-13/?pageNumber=",
        f"{VARIANT}?merchantId=7001",  # an offer link is built, never requested
        f"{CATALOG}/iphone-13",
        f"{CATALOG}/IPHONE-13/",
        "https://getmobil.com/ara?term=iphone",
        "https://getmobil.com/satin-al/aksesuar/kilif/",
        "http://getmobil.com/satin-al/cep-telefonu/iphone-ios-telefonlar/apple/",
        "https://getmobil.com:8443/satin-al/cep-telefonu/iphone-ios-telefonlar/apple/",
        "https://www.getmobil.com/satin-al/cep-telefonu/iphone-ios-telefonlar/apple/",
        f"{CATALOG}/#reviews",
    ],
)
def test_getmobil_refuses_sort_filter_and_unknown_urls(url):
    with pytest.raises(UrlNotAllowed):
        getmobil.check_url(url)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "cep-telefonu/iphone-ios-telefonlar/apple/iphone-13/apple-iphone-13-128-gb-mavi-900101/?merchantId=7001",
            VARIANT,
        ),
        (
            "cep-telefonu/iphone-ios-telefonlar/apple/iphone-13/apple-iphone-13-128-gb-mavi-900101?merchantId=7001",
            VARIANT,
        ),
        ("/satin-al/cep-telefonu/iphone-ios-telefonlar/apple/iphone-13/apple-iphone-13-128-gb-mavi-900101", VARIANT),
    ],
)
def test_getmobil_variant_urls_drop_the_query_and_add_the_trailing_slash(path, expected):
    assert getmobil.variant_url(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "cep-telefonu/aksesuar/kilif/ornek-kilif-900199/",
        "cep-telefonu/iphone-ios-telefonlar/apple/iphone-13/",
        "cep-telefonu/iphone-ios-telefonlar/apple/iphone-13/../../../ara-1/",
    ],
)
def test_getmobil_refuses_paths_that_are_not_variant_pages(path):
    with pytest.raises(UrlNotAllowed):
        getmobil.variant_url(path)


def test_getmobil_builders_only_produce_allowed_urls():
    assert getmobil.listing_url("iphone-13", 3) == f"{CATALOG}/iphone-13/?pageNumber=3"
    assert getmobil.offer_url(VARIANT, 7001) == f"{VARIANT}?merchantId=7001"
    with pytest.raises(UrlNotAllowed):
        getmobil.listing_url("iphone-13/?sortStrategy=price&x", 1)


@pytest.mark.parametrize(
    ("url", "form"),
    [
        ("https://easycep.com/kategori/apple-2?page=1", "listing"),
        ("https://easycep.com/kategori/apple-2?page=6", "listing"),
        ("https://easycep.com/apple-iphone-13-128-gb-mavi-cep-telefonu-700001", "unit"),
        ("https://easycep.com/iphone-15-128-gb-mavi-cep-telefonu-700002", "unit"),
    ],
)
def test_easycep_allows_its_page_forms(url, form):
    assert easycep.check_url(url) == form


@pytest.mark.parametrize(
    "url",
    [
        # robots.txt has "*?orderby*", which does not catch "&orderby": the allowlist must.
        "https://easycep.com/kategori/apple-2?page=2&orderby=1",
        "https://easycep.com/kategori/apple-2?orderby=1",
        "https://easycep.com/kategori/apple-2?page=1&filter=apple",
        "https://easycep.com/kategori/apple-2?keyword=iphone",
        "https://easycep.com/kategori/apple-2?_rsc=1",
        "https://easycep.com/kategori/iphone-13-116",
        "https://easycep.com/apple-iphone-13-128-gb-mavi-cep-telefonu-700001?ref=x",
        "https://easycep.com/sepet/add-to-cart/5",
        "https://easycep.com/api/v1/staticPage/getStaticPage",
    ],
)
def test_easycep_refuses_sort_filter_and_unknown_urls(url):
    with pytest.raises(UrlNotAllowed):
        easycep.check_url(url)


def test_easycep_builders_only_produce_allowed_urls():
    assert easycep.listing_url(2) == "https://easycep.com/kategori/apple-2?page=2"
    unit = "https://easycep.com/apple-iphone-13-128-gb-mavi-cep-telefonu-700001"
    assert easycep.unit_url("/apple-iphone-13-128-gb-mavi-cep-telefonu-700001") == unit
    with pytest.raises(UrlNotAllowed):
        easycep.unit_url("/kategori/apple-2?page=2&orderby=1")


TY = "https://www.trendyol.com"
TY_CATEGORY = f"{TY}/apple-yenilenmis-cep-telefonu-x-b101470-c109461"
TY_IPHONE = f"{TY}/iphone-ios-cep-telefonlari-x-c164462"  # new phones (and some misfiled refurbished ones)
TY_MODEL = f"{TY}/iphone-13-cep-telefonu-x-c103498-a292-v613184"  # every iPhone 13, new and refurbished
TY_PRODUCT = f"{TY}/apple/yenilenmis-iphone-13-128-gb-mavi-ornek-urun-a-kalite-p-900000101"


@pytest.mark.parametrize(
    ("url", "form"),
    [
        (TY_CATEGORY, "listing"),
        (f"{TY_CATEGORY}?pi=2", "listing"),
        (f"{TY_CATEGORY}?pi=24", "listing"),
        (TY_IPHONE, "listing"),
        (f"{TY_IPHONE}?pi=5", "listing"),
        (TY_MODEL, "model"),
        (f"{TY}/iphone-15-pro-max-cep-telefonu-x-c103498-a292-v1218522", "model"),
        (TY_PRODUCT, "product"),
        (f"{TY}/apple/iphone-13-pembe-128gb-xyz1tu-a-ornek-a-kalite-p-1200000001", "product"),
    ],
)
def test_trendyol_allows_its_page_forms(url, form):
    assert trendyol.check_url(url) == form


@pytest.mark.parametrize(
    "url",
    [
        f"{TY_CATEGORY}?sst=PRICE_BY_ASC",
        f"{TY_CATEGORY}?pi=2&sst=PRICE_BY_ASC",
        f"{TY_CATEGORY}?q=iphone",
        f"{TY_CATEGORY}?pi=0",
        f"{TY_CATEGORY}/",
        f"{TY_MODEL}?pi=2",  # model pages: page 1 only, robots.txt disallows ?pi= there
        f"{TY_MODEL}?sst=PRICE_BY_ASC",
        f"{TY_MODEL}/",
        f"{TY}/iphone-ios-cep-telefonlari-x-c164462-a292-v613184",  # a combined form the site was not seen to use
        f"{TY}/samsung-galaxy-s24-cep-telefonu-x-c103498-a292-v613184",
        f"{TY}/iphone-13-cep-telefonu-x-c103498-a292-v0",
        f"{TY}/iphone-13-cep-telefonu-x-c103498-a348-v1",  # another attribute (colour)
        f"{TY_IPHONE}?sst=PRICE_BY_ASC",
        f"{TY_IPHONE}?pi=2&wc=164462",
        f"{TY_IPHONE}?q=iphone",
        f"{TY}/android-cep-telefonu-x-c164461",
        f"{TY}/apple-x-b101470",
        f"{TY_PRODUCT}/saticilar",
        f"{TY_PRODUCT}/yorumlar",
        f"{TY_PRODUCT}?boutiqueId=7&merchantId=7001",
        f"{TY_PRODUCT}?merchantId=7001",  # an offer link is built, never requested
        f"{TY}/sr?q=iphone",
        f"{TY}/sr?mid=7001",
        f"{TY}/ornek-aksesuar/iphone-13-uyumlu-kilif-p-900000191",
        f"{TY}/magaza/ornek-magaza-m-7001",
        "https://apigw.trendyol.com/discovery-sfint-search-service/api/search/products/?pi=1&pageSize=36",
        "https://trendyol.com/apple-yenilenmis-cep-telefonu-x-b101470-c109461",
        "http://www.trendyol.com/apple-yenilenmis-cep-telefonu-x-b101470-c109461",
    ],
)
def test_trendyol_refuses_sorting_search_seller_pages_api_and_unknown_urls(url):
    with pytest.raises(UrlNotAllowed):
        trendyol.check_url(url)


def test_trendyol_builders_only_produce_allowed_urls():
    assert trendyol.listing_url("refurbished", 1) == TY_CATEGORY  # page 1 is the bare path
    assert trendyol.listing_url("refurbished", 3) == f"{TY_CATEGORY}?pi=3"
    assert trendyol.listing_url("new", 1) == TY_IPHONE
    assert trendyol.listing_url("new", 2) == f"{TY_IPHONE}?pi=2"
    assert trendyol.model_url(613184, "iPhone 13") == TY_MODEL
    with pytest.raises(UrlNotAllowed):
        trendyol.model_url(613184, "Galaxy S24")
    card_path = "/apple/yenilenmis-iphone-13-128-gb-mavi-ornek-urun-a-kalite-p-900000101?boutiqueId=7&merchantId=7001"
    assert trendyol.product_url(card_path) == TY_PRODUCT
    assert trendyol.product_url(f"{TY_PRODUCT}#yorumlar") == TY_PRODUCT
    assert trendyol.offer_url(TY_PRODUCT, 1234567) == f"{TY_PRODUCT}?merchantId=1234567"  # 7-digit merchant ids
    for path in ("/apple/ornek-urun-p-900000101/saticilar", "/ornek-aksesuar/kilif-p-900000191", "/sr?q=iphone"):
        with pytest.raises(UrlNotAllowed):
            trendyol.product_url(path)
