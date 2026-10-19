import json

import httpx
import pytest
from helpers import FakeSite

from price_radar.ingest import probe
from price_radar.ingest.http_client import CrawlPolicy

POLICY = CrawlPolicy(user_agent="TestBot/1.0 (+https://example.org)")
PRODUCT_PAGE = """<html><head><title>Yenilenmiş iPhone 13 128 GB</title>
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "Product", "name": "iPhone 13",
 "offers": {"@type": "AggregateOffer", "lowPrice": 25450.5}}</script>
<script type="application/ld+json">{"@graph": [{"@type": "BreadcrumbList"}]}</script>
</head><body>iPhone 13 - 25.450,50 TL - 26.100 TL <script>self.__next_f.push([1,""])</script></body></html>"""


def run(site: FakeSite, urls: list[str]) -> list[dict]:
    return probe.probe(urls, POLICY, transport=site.transport(), sleep=lambda seconds: None)


def test_a_readable_page_is_described_without_its_content():
    site = FakeSite("https://shop.example", {"/yenilenmis": PRODUCT_PAGE})
    (result,) = run(site, ["https://shop.example/yenilenmis"])
    assert (result["outcome"], result["status"], result["redirects"]) == ("ok", 200, 0)
    assert result["json_ld"] == ["AggregateOffer", "BreadcrumbList", "Product"]
    assert (result["next_flight"], result["next_data"]) == (True, False)
    assert (result["iphone_mentions"], result["tl_prices"]) == (3, 2)
    assert result["title"] == "Yenilenmiş iPhone 13 128 GB"
    assert "25.450,50" not in json.dumps(result)  # counts only, no page content


def test_a_block_keeps_the_status_and_the_bot_management_signs():
    blocked = httpx.Response(403, headers={"server": "AkamaiGHost", "set-cookie": "_abck=x; path=/"}, text="Denied")
    site = FakeSite("https://shop.example", {"/yenilenmis": blocked, "/second": PRODUCT_PAGE})
    first, second = run(site, ["https://shop.example/yenilenmis", "https://shop.example/second"])
    assert first["outcome"].startswith("stopped: bot challenge or HTTP 403")
    assert first["status"] == 403
    assert first["bot_management"] == ["akamai", "akamai bot manager"]
    assert second["outcome"].startswith("stopped:")  # the host is not asked again
    assert site.requested == ["/robots.txt", "/yenilenmis"]


def test_robots_txt_is_obeyed():
    site = FakeSite(
        "https://shop.example", {"/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /ara\n")}
    )
    (result,) = run(site, ["https://shop.example/ara?q=iphone"])
    assert result["outcome"] == "disallowed by robots.txt"
    assert site.requested == ["/robots.txt"]


def test_each_host_gets_its_own_client_and_robots_txt():
    one = FakeSite("https://one.example", {"/a": PRODUCT_PAGE})
    two = FakeSite("https://two.example", {"/b": PRODUCT_PAGE})
    transport = httpx.MockTransport(
        lambda request: (one if request.url.host == "one.example" else two).handler(request)
    )
    results = probe.probe(
        ["https://one.example/a", "https://two.example/b"], POLICY, transport=transport, sleep=lambda seconds: None
    )
    assert [r["outcome"] for r in results] == ["ok", "ok"]
    assert one.requested == ["/robots.txt", "/a"] and two.requested == ["/robots.txt", "/b"]


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"server": "cloudflare", "cf-ray": "1"}, ["cloudflare"]),
        ({"cf-ray": "1", "set-cookie": "__cf_bm=x; path=/"}, ["cloudflare", "cloudflare bot management"]),
        ({"x-datadome": "protected"}, ["datadome"]),
        ({"x-cdn": "Incapsula"}, ["imperva"]),
        ({"set-cookie": "_pxhd=x"}, ["perimeterx"]),
        ({"server": "nginx"}, []),
    ],
)
def test_bot_management_signs(headers, expected):
    assert probe.bot_management(httpx.Headers(headers)) == expected


def test_annotation_is_one_line_per_url():
    site = FakeSite("https://shop.example", {"/yenilenmis": PRODUCT_PAGE})
    (result,) = run(site, ["https://shop.example/yenilenmis"])
    line = probe.annotation(result, "github")
    assert line.startswith("::notice title=probe github shop.example/yenilenmis::ok; HTTP 200; ")
    assert "JSON-LD AggregateOffer,BreadcrumbList,Product" in line and "\n" not in line


def test_offer_availability_is_counted():
    page = """<script type="application/ld+json">{"@type": "ItemList", "itemListElement": [
      {"@type": "ListItem", "item": {"@type": "Product", "offers": {"@type": "Offer",
        "availability": "https://schema.org/OutOfStock"}}},
      {"@type": "ListItem", "item": {"@type": "Product", "offers": {"@type": "Offer",
        "availability": "https://schema.org/InStock"}}},
      {"@type": "ListItem", "item": {"@type": "Product", "offers": {"@type": "Offer",
        "availability": "https://schema.org/OutOfStock"}}}]}</script>"""
    markers = probe.page_markers(page)
    assert markers["offer_availability"] == {"InStock": 1, "OutOfStock": 2}
    assert markers["json_ld"] == ["ItemList", "ListItem", "Offer", "Product"]
