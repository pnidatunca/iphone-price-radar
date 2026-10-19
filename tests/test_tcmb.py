import json
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
from helpers import FIXTURES, NOW, FakeSite

from price_radar.fx import tcmb
from price_radar.ingest import urls

BULLETIN = (FIXTURES / "tcmb" / "bulletin.xml").read_bytes()  # synthetic values, structure of a real bulletin
FRIDAY = date(2026, 10, 9)
FRIDAY_PATH = "/kurlar/202610/09102026.xml"


def parse(content: bytes = BULLETIN, *, expected: date = FRIDAY, currencies=("USD",)) -> tcmb.Bulletin:
    return tcmb.parse_bulletin(
        content, tcmb.archive_url(expected), expected=expected, fetched_at=NOW, currencies=currencies
    )


def rate(announced_on: date, selling: str) -> tcmb.FxRate:
    return tcmb.FxRate(
        currency="USD",
        announced_on=announced_on,
        valid_from=announced_on + timedelta(days=1),
        unit=1,
        forex_selling=Decimal(selling),
        source_url=tcmb.archive_url(announced_on),
        fetched_at=NOW,
    )


def site(**routes) -> FakeSite:
    return FakeSite("https://www.tcmb.gov.tr", {FRIDAY_PATH: BULLETIN.decode("utf-8"), **routes})


def cli(args: list[str], fake: FakeSite) -> int:
    return tcmb.main(args, transport=fake.transport(), sleep=lambda seconds: None, now=lambda: NOW)


def test_archive_url_is_the_only_allowed_form():
    url = tcmb.archive_url(FRIDAY)
    assert url == "https://www.tcmb.gov.tr/kurlar/202610/09102026.xml"
    assert urls.check(url, tcmb.HOST, tcmb.URL_FORMS) == "bulletin"
    for other in (
        "https://www.tcmb.gov.tr/kurlar/today.xml",
        "https://www.tcmb.gov.tr/kurlar/202610/09102026.xml?page=1",
        "https://tcmb.gov.tr/kurlar/202610/09102026.xml",
    ):
        with pytest.raises(urls.UrlNotAllowed):
            urls.check(other, tcmb.HOST, tcmb.URL_FORMS)


def test_bulletin_gives_the_requested_currencies_as_published():
    bulletin = parse(currencies=("USD", "JPY", "GBP"))
    assert (bulletin.announced_on, bulletin.bulletin_no, bulletin.missing) == (FRIDAY, "2026/999", ("GBP",))
    usd, jpy = bulletin.rates
    assert (usd.currency, usd.unit, usd.forex_buying, usd.forex_selling) == (
        "USD",
        1,
        Decimal("40.1000"),
        Decimal("40.2000"),
    )
    assert (usd.banknote_buying, usd.banknote_selling) == (Decimal("40.0700"), Decimal("40.2600"))
    assert usd.valid_from == date(2026, 10, 10)  # announced Friday 15:30, valid from Saturday
    assert usd.source_url == "https://www.tcmb.gov.tr/kurlar/202610/09102026.xml"
    assert (usd.parser_version, usd.fetched_at) == ("tcmb-1", NOW)
    assert (jpy.unit, jpy.forex_selling) == (100, Decimal("27.3000"))  # lira per 100 yen, kept as published


def test_a_currency_without_a_selling_rate_is_refused():
    with pytest.raises(tcmb.BulletinError, match="XDR"):
        parse(currencies=("XDR",))


def test_the_bulletin_of_another_day_is_refused():
    with pytest.raises(tcmb.BulletinError, match="2026-10-09 was served for 2026-10-08"):
        parse(expected=date(2026, 10, 8))


@pytest.mark.parametrize(
    "content",
    [
        b"<html><head><title>Sayfa Goruntulenemedi - Page Not Found</title></head></html>",
        b"<Kurlar><Currency/></Kurlar>",
        BULLETIN.replace(b'Date="10/09/2026"', b'Date="10/08/2026"'),
        BULLETIN.replace(b"<ForexBuying>40.1000</ForexBuying>", b"<ForexBuying>41.0000</ForexBuying>"),
        BULLETIN.replace(b"<ForexSelling>40.2000</ForexSelling>", b"<ForexSelling>40,2000</ForexSelling>"),
        BULLETIN[:200],
    ],
    ids=["html", "other-root", "inconsistent-dates", "buying-above-selling", "comma-decimal", "truncated"],
)
def test_anything_but_a_valid_bulletin_is_refused(content):
    with pytest.raises(tcmb.BulletinError):
        parse(content)


def test_a_rate_applies_from_the_day_after_its_announcement():
    thursday, friday, monday = (
        rate(date(2026, 10, 8), "40.10"),
        rate(FRIDAY, "40.20"),
        rate(date(2026, 10, 12), "40.30"),
    )
    rates = [monday, thursday, friday]
    assert tcmb.rate_for(rates, date(2026, 10, 8)) is None  # nothing announced before
    assert tcmb.rate_for(rates, FRIDAY) == thursday  # Friday's own bulletin comes out at 15:30
    for weekend_or_monday in (date(2026, 10, 10), date(2026, 10, 11), date(2026, 10, 12)):
        assert tcmb.rate_for(rates, weekend_or_monday) == friday
    assert tcmb.rate_for(rates, date(2026, 10, 13)) == monday
    assert tcmb.rate_for(rates, date(2026, 10, 13), currency="EUR") is None


def test_bulletin_days_skip_weekends_and_stored_days():
    days = tcmb.bulletin_days(FRIDAY, date(2026, 10, 13), known=frozenset({date(2026, 10, 12)}))
    assert days == [FRIDAY, date(2026, 10, 13)]


def test_collect_records_days_without_a_bulletin_and_failures():
    fake = site(**{"/kurlar/202610/08102026.xml": "<html>Bakım çalışması</html>"})  # 07.10 answers 404
    ctx = fake.context()
    rates = list(tcmb.collect(ctx, [date(2026, 10, 7), date(2026, 10, 8), FRIDAY]))
    assert [(r.currency, r.announced_on) for r in rates] == [("USD", FRIDAY)]
    assert ctx.stats.counters["bulletins"] == 1
    assert ctx.stats.discovery["days_without_bulletin"] == ["2026-10-07"]
    assert ctx.stats.failure_count == 1 and "root element is 'html'" in ctx.stats.failures[0]["error"]
    assert fake.requested == ["/robots.txt", "/kurlar/202610/07102026.xml", "/kurlar/202610/08102026.xml", FRIDAY_PATH]


def test_cli_writes_the_rates_and_a_summary(tmp_path, capsys):
    fake = site()
    # NOW is Monday 12.10, 03:17 in Turkey: the last three days up to yesterday are Friday, Saturday and Sunday.
    assert cli(["--out", str(tmp_path), "--days", "3", "--markdown"], fake) == 0
    assert fake.requested == ["/robots.txt", FRIDAY_PATH]  # no request for the weekend
    assert sorted(path.name for path in tmp_path.iterdir()) == ["rates.jsonl", "run_summary.json"]
    lines = (tmp_path / "rates.jsonl").read_text("utf-8").splitlines()
    assert [tcmb.FxRate.model_validate_json(line).forex_selling for line in lines] == [Decimal("40.2000")]
    summary = json.loads((tmp_path / "run_summary.json").read_text("utf-8"))
    assert summary["stop_reason"] == "completed"
    assert summary["days"] == {"from": "2026-10-09", "to": "2026-10-11", "to_check": 1, "already_stored": 0}
    assert (summary["bulletins"], summary["rates"], summary["requests"]["made"]) == (1, 1, 2)
    assert summary["latest"] == {"announced_on": "2026-10-09", "currency": "USD", "forex_selling": "40.2000"}
    assert "| Latest | USD 40.2000, announced 2026-10-09 |" in capsys.readouterr().out


def test_cli_requests_nothing_when_every_bulletin_is_stored(tmp_path):
    known = tmp_path / "known.txt"
    known.write_text("# stored\n2026-10-09\n\n", encoding="utf-8")
    fake = site()
    assert cli(["--out", str(tmp_path / "out"), "--days", "3", "--known-dates", str(known)], fake) == 0
    assert fake.requested == []  # not even robots.txt
    summary = json.loads((tmp_path / "out" / "run_summary.json").read_text("utf-8"))
    assert (summary["days"]["to_check"], summary["days"]["already_stored"], summary["rates"]) == (0, 1, 0)


def test_cli_stops_on_a_block_and_says_so(tmp_path):
    fake = site(**{FRIDAY_PATH: httpx.Response(403, text="Forbidden")})
    assert cli(["--out", str(tmp_path), "--days", "3"], fake) == 1
    summary = json.loads((tmp_path / "run_summary.json").read_text("utf-8"))
    assert summary["stop_reason"].startswith("bot challenge or HTTP 403")


@pytest.mark.parametrize(
    "args",
    [["--to", "2026-10-12"], ["--from", "2026-10-11", "--to", "2026-10-09"], ["--known-dates", "{bad}"]],
    ids=["today", "from-after-to", "bad-known-dates"],
)
def test_cli_usage_errors(tmp_path, args):
    bad = tmp_path / "bad.txt"
    bad.write_text("9.10.2026\n", encoding="utf-8")
    args = [arg.replace("{bad}", str(bad)) for arg in args]
    fake = site()
    assert cli(["--out", str(tmp_path / "out"), *args], fake) == 2
    assert fake.requested == []


def test_days_and_from_cannot_be_combined(tmp_path):
    with pytest.raises(SystemExit):
        cli(["--out", str(tmp_path), "--days", "3", "--from", "2026-10-01"], site())
