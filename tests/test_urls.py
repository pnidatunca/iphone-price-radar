import pytest

from price_radar.ingest.sources import easycep, getmobil
from price_radar.ingest.urls import UrlNotAllowed

CATALOG = "https://getmobil.com/satin-al/cep-telefonu/apple"
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
        "http://getmobil.com/satin-al/cep-telefonu/apple/",
        "https://getmobil.com:8443/satin-al/cep-telefonu/apple/",
        "https://www.getmobil.com/satin-al/cep-telefonu/apple/",
        "https://getmobil.com/satin-al/cep-telefonu/iphone-ios-telefonlar/apple/",  # the path before 15.10.2026
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
            "cep-telefonu/apple/iphone-13/apple-iphone-13-128-gb-mavi-900101/?merchantId=7001",
            VARIANT,
        ),
        (
            "cep-telefonu/apple/iphone-13/apple-iphone-13-128-gb-mavi-900101?merchantId=7001",
            VARIANT,
        ),
        ("/satin-al/cep-telefonu/apple/iphone-13/apple-iphone-13-128-gb-mavi-900101", VARIANT),
    ],
)
def test_getmobil_variant_urls_drop_the_query_and_add_the_trailing_slash(path, expected):
    assert getmobil.variant_url(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "cep-telefonu/aksesuar/kilif/ornek-kilif-900199/",
        "cep-telefonu/apple/iphone-13/",
        "cep-telefonu/apple/iphone-13/../../../ara-1/",
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
