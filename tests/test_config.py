from price_radar.config import database_url, load_settings, secret


def test_settings_file_is_valid():
    settings = load_settings()
    assert settings.crawler.user_agent.startswith("IphonePriceRadar/")
    assert "github.com" not in settings.crawler.user_agent  # no link to a personal account (owner, 15.10.2026)
    assert "@" not in settings.crawler.user_agent  # no e-mail address in the identity (decision K5)
    assert set(settings.sources) >= {"getmobil", "trendyol", "tcmb"}
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


def test_database_url_prefers_the_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("NEON_DATABASE_URL=postgresql://from-file\n", encoding="utf-8")
    monkeypatch.setenv("NEON_DATABASE_URL", "postgresql://from-env")
    assert database_url(env_file) == "postgresql://from-env"


def test_database_url_falls_back_to_the_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text('# local\nOTHER=1\nNEON_DATABASE_URL="postgresql://from-file"\n', encoding="utf-8")
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    assert database_url(env_file) == "postgresql://from-file"
    assert database_url(tmp_path / "missing.env") is None


def test_any_secret_is_read_the_same_way(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_CHAT_ID=42\n", encoding="utf-8")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert secret("TELEGRAM_CHAT_ID", env_file) == "42"
