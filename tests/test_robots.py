import pytest

from price_radar.ingest.robots import is_allowed, parse_robots

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
Disallow: /*?q=
Disallow: /sr?
Disallow: /*/*-p-*/saticilar
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
    rules = parse_robots("User-agent: BadBot\nDisallow: /\n\nUser-agent: *\nDisallow: /private\n")
    assert is_allowed(rules, "/public")
    assert not is_allowed(rules, "/private/x")


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
