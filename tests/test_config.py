from price_radar.config import load_settings


def test_settings_file_is_valid():
    settings = load_settings()
    assert settings.crawler.user_agent.startswith("IphonePriceRadar/")
    assert "github.com" not in settings.crawler.user_agent  # no link to a personal account (owner, 15.10.2026)
    assert "@" not in settings.crawler.user_agent  # no e-mail address in the identity (decision K5)
    assert set(settings.sources) >= {"getmobil", "easycep"}
    assert settings.crawler.delay_seconds == (2.0, 3.5)


def test_scope_starts_at_iphone_x():
    scope = load_settings().scope
    for model in ("iPhone X", "iPhone XR", "iPhone XS Max", "iPhone SE (2020)", "iPhone SE (2022)", "iPhone 16e"):
        assert scope.includes(model), model
    for model in ("iPhone 6", "iPhone 7 Plus", "iPhone 8", "iphone 8 plus", "iPhone SE (2016)"):
        assert not scope.includes(model), model


def test_policy_mirrors_the_settings():
    crawler = load_settings().crawler
    policy = crawler.policy()
    assert policy.user_agent == crawler.user_agent
    assert policy.max_requests == crawler.max_requests_per_source
    assert policy.delay_seconds == crawler.delay_seconds
