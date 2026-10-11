from price_radar.config import load_settings


def test_settings_file_is_valid():
    settings = load_settings()
    assert settings.crawler.user_agent.startswith("IphonePriceRadar/")
    assert "github.com/pnidatunca/iphone-price-radar" in settings.crawler.user_agent
    assert set(settings.sources) >= {"getmobil", "easycep"}
    assert settings.crawler.delay_seconds == (2.0, 3.5)


def test_policy_mirrors_the_settings():
    crawler = load_settings().crawler
    policy = crawler.policy()
    assert policy.user_agent == crawler.user_agent
    assert policy.max_requests == crawler.max_requests_per_source
    assert policy.delay_seconds == crawler.delay_seconds
