import pytest

from price_radar.ingest.robots import is_allowed, parse_robots
from price_radar.ingest.sources import trendyol

# Trimmed copies of the real files (fetched 2026-10-11), keeping the rules that matter for the adapters.
GETMOBIL = """
User-Agent: *
Disallow: /satin-al-v1
Disallow: /satin-al/aksesuar
Disallow: /*?minPrice=*
Disallow: /*&minPrice=*
Disallow: /*?condition=*
Disallow: /*&condition=*
Disallow: /ara
"""
EASYCEP = """
User-agent: *
Disallow: *add-to-cart*
Disallow: *minPrice*
Disallow: *keyword*
Disallow: *?filter*
Disallow: *?orderby*
Sitemap: https://easycep.com/sitemap.xml
"""
TRENDYOL = """
User-agent: *
User-agent: Googlebot
Disallow: /*?mid=
Disallow: /*?pid=
Disallow: /*?q=
Disallow: /*?sst=
Disallow: /*?wc=
Disallow: /*&mid=
Disallow: /*&pid=
Disallow: /*&sst=
Disallow: /*&wc=
Disallow: /*disablemerchant=
Disallow: /*satici=
Disallow: /*seller=
Disallow: /*/*/*-p-*/merchant-questions
Disallow: /*/*-p-*/saticilar
Disallow: /diger-saticilar
Disallow: /gw/
Disallow: /sr?
Disallow: /sr/
Disallow: /sr$
Disallow: */*-x-b*-a*-v*
Disallow: */sr/*-x-b*-c*-a*-v*
Allow: /*-x-b*-c*-a*-v*

# Pagination Rules
Disallow: /*-x-*-a*-v*?pi=
"""
LETGO_LIKE = """
User-agent: *
Allow: /
Disallow: /api/
User-agent: AhrefsBot
Disallow: /
"""


@pytest.mark.parametrize(
    ("robots", "path", "expected"),
    [
        (GETMOBIL, "/satin-al/cep-telefonu/iphone-ios-telefonlar/apple/iphone-13/", True),
        (GETMOBIL, "/satin-al/x/apple-iphone-13-128-gb-gece-yarisi-797/?merchantId=1", True),
        (GETMOBIL, "/satin-al/x/?minPrice=100", False),
        (GETMOBIL, "/satin-al/x/?a=1&minPrice=100", False),
        (GETMOBIL, "/satin-al/x/?condition=2", False),
        (GETMOBIL, "/satin-al-v1/abc", False),
        (GETMOBIL, "/satin-al/aksesuar/kilif", False),
        (GETMOBIL, "/ara?term=iphone", False),
        (EASYCEP, "/apple-iphone-14-128-gb-yildiz-isigi-cep-telefonu-41965", True),
        (EASYCEP, "/sepet/add-to-cart/5", False),
        (EASYCEP, "/cep-telefonu?filter=apple", False),
        (TRENDYOL, "/apple/iphone-13-p-123456", True),
        (TRENDYOL, "/sr?q=iphone", False),
        (TRENDYOL, "/apple/iphone-13-p-123456/saticilar", False),
        (LETGO_LIKE, "/item/123", True),
        (LETGO_LIKE, "/api/items", False),
    ],
)
def test_real_site_rules(robots, path, expected):
    assert is_allowed(parse_robots(robots), path) is expected


def test_groups_for_other_bots_are_ignored():
    robots = "User-agent: BadBot\nDisallow: /\n\nUser-agent: *\nDisallow: /private\n"
    for token in ("", "IphonePriceRadar"):
        rules = parse_robots(robots, token)
        assert is_allowed(rules, "/public")
        assert not is_allowed(rules, "/private/x")


def test_group_for_our_bot_replaces_the_star_group():
    robots = "User-agent: *\nDisallow: /private\n\nUser-agent: IphonePriceRadar\nDisallow: /\n"
    assert not is_allowed(parse_robots(robots, "IphonePriceRadar"), "/anything")
    assert is_allowed(parse_robots(robots), "/anything")


@pytest.mark.parametrize("agent_line", ["iphonepriceradar", "IPHONEPRICERADAR", "IphonePriceRadar/0.1"])
def test_our_group_matches_case_insensitively_and_ignores_versions(agent_line):
    rules = parse_robots(f"User-agent: *\nAllow: /\n\nUser-agent: {agent_line}\nDisallow: /x\n", "IphonePriceRadar/0.1")
    assert not is_allowed(rules, "/x/1")
    assert is_allowed(rules, "/y")


def test_our_group_can_allow_more_than_the_star_group():
    robots = "User-agent: *\nDisallow: /\n\nUser-agent: IphonePriceRadar\nAllow: /\n"
    assert is_allowed(parse_robots(robots, "IphonePriceRadar"), "/item")


def test_longest_match_wins_and_allow_wins_ties():
    rules = parse_robots("User-agent: *\nDisallow: /shop\nAllow: /shop/public\nDisallow: /tie\nAllow: /tie\n")
    assert not is_allowed(rules, "/shop/item")
    assert is_allowed(rules, "/shop/public/item")
    assert is_allowed(rules, "/tie")


def test_dollar_anchors_the_end():
    rules = parse_robots("User-agent: *\nDisallow: /items$\n")
    assert not is_allowed(rules, "/items")
    assert is_allowed(rules, "/items/1")


def test_empty_disallow_allows_everything():
    assert is_allowed(parse_robots("User-agent: *\nDisallow:\n"), "/anything")


def test_no_rules_means_allowed():
    assert is_allowed(parse_robots(""), "/anything")


def trendyol_path(url: str) -> str:
    return url.removeprefix(trendyol.BASE_URL)


def test_trendyol_rules_allow_every_url_the_adapter_requests_or_builds():
    rules = parse_robots(TRENDYOL, "IphonePriceRadar")
    for category in trendyol.CATEGORY_PATHS:  # the refurbished and the iPhone category
        for number in (1, 2, 5, 24):
            assert is_allowed(rules, trendyol_path(trendyol.listing_url(category, number))), (category, number)
    assert is_allowed(rules, trendyol_path(trendyol.model_url(613184, "iPhone 13")))
    product = trendyol.product_url("/apple/iphone-15-ornek-urun-128-gb-siyah-p-900000104?boutiqueId=7&merchantId=7")
    assert is_allowed(rules, trendyol_path(product))
    assert is_allowed(rules, trendyol_path(trendyol.offer_url(product, 1234567)))


@pytest.mark.parametrize(
    "path",
    [
        "/iphone-13-cep-telefonu-x-c103498-a292-v613184?pi=2",  # model pages after page 1
        "/iphone-ios-cep-telefonlari-x-c164462?sst=PRICE_BY_ASC",
        "/iphone-ios-cep-telefonlari-x-c164462?pi=2&wc=164462",
        "/apple-yenilenmis-cep-telefonu-x-b101470-c109461?q=iphone",
        "/apple/iphone-15-ornek-urun-128-gb-siyah-p-900000104/saticilar",
        "/apple/iphone-15-ornek-urun-128-gb-siyah-p-900000104?mid=7",
        "/sr?q=iphone",
    ],
)
def test_trendyol_rules_disallow_what_the_allowlist_refuses_anyway(path):
    assert not is_allowed(parse_robots(TRENDYOL, "IphonePriceRadar"), path)
