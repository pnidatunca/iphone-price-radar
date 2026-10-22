from datetime import UTC, date, datetime, timedelta

from price_radar.monitoring.health import RunRecord, annotations, evaluate, message

TODAY = date(2026, 10, 22)


def run(day: date, offers: int = 1500, **changes) -> RunRecord:
    values = {
        "snapshot_date": day,
        "started_at": datetime(day.year, day.month, day.day, 6, 30, tzinfo=UTC),
        "exit_code": 0,
        "stop_reason": "completed",
        "offers": offers,
        "requests": 700,
        "budget": 1500,
        "rejected": 0,
        "warnings": 1,
        "failures": 0,
        "bot_pages": 0,
    }
    return RunRecord(**(values | changes))


def week(offers: int = 1500) -> list[RunRecord]:
    return [run(TODAY - timedelta(days=n), offers) for n in range(7, 0, -1)]


def titles(findings):
    return [(f.level, f.title) for f in findings]


def test_a_normal_day_has_no_findings():
    findings, summary = evaluate([*week(), run(TODAY, 1517)], "getmobil", TODAY, fx_latest=TODAY - timedelta(days=1))
    assert findings == []
    assert summary == "getmobil 22.10: 1,517 offers (7-day average 1,500), 700 requests, warnings 1, failures 0"


def test_a_day_without_any_run_is_an_error():
    findings, summary = evaluate(week(), "getmobil", TODAY)
    assert titles(findings) == [("error", "No complete getmobil run today")]
    assert findings[0].message == "22.10.2026: no run started"
    assert summary == "getmobil 22.10: no complete run"


def test_a_failed_run_is_named_with_its_stop_reason():
    failed = run(TODAY, 0, exit_code=1, stop_reason="no offers collected")
    findings, _ = evaluate([*week(), failed], "getmobil", TODAY)
    assert findings[0].message == "22.10.2026: last attempt stopped: no offers collected (exit 1)"


def test_a_later_complete_run_of_the_day_settles_it():
    failed = run(TODAY, 0, exit_code=1, stop_reason="crashed: X")
    findings, _ = evaluate([*week(), failed, run(TODAY, 1490)], "getmobil", TODAY)
    assert findings == []


def test_a_missing_yesterday_is_an_error_once_there_is_history():
    runs = [r for r in week() if r.snapshot_date != TODAY - timedelta(days=1)] + [run(TODAY)]
    assert titles(evaluate(runs, "getmobil", TODAY)[0]) == [("error", "No getmobil run yesterday")]
    # On the first two days of data there is nothing to miss yet.
    assert evaluate([run(TODAY)], "getmobil", TODAY)[0] == []


def test_offers_below_half_the_average_are_an_error():
    findings, _ = evaluate([*week(1600), run(TODAY, 700)], "getmobil", TODAY)
    assert titles(findings) == [("error", "getmobil offers dropped")]
    assert findings[0].message.startswith("700 offers against a 7-day average of 1600")
    assert evaluate([*week(1600), run(TODAY, 801)], "getmobil", TODAY)[0] == []


def test_bot_flags_budget_rejections_and_stale_rates():
    today = run(TODAY, 1500, bot_pages=3, requests=1400, rejected=90)
    findings, _ = evaluate([*week(), today], "getmobil", TODAY, fx_latest=TODAY - timedelta(days=5))
    assert titles(findings) == [
        ("error", "getmobil flags the crawler"),
        ("warning", "getmobil request budget nearly spent"),
        ("warning", "getmobil rejected offers"),
        ("warning", "TCMB rates are stale"),
    ]


def test_the_message_and_the_annotations():
    findings, summary = evaluate(week(), "getmobil", TODAY)
    text = message(findings, summary, "after the nightly run")
    assert text.splitlines() == [
        "[ALERT] iPhone Price Radar, after the nightly run",
        "getmobil 22.10: no complete run",
        "- No complete getmobil run today: 22.10.2026: no run started",
    ]
    assert message([], "getmobil 22.10: 1,500 offers", "x").startswith("[OK] ")
    assert annotations(findings, summary) == [
        "::notice title=Health::getmobil 22.10: no complete run",
        "::error title=No complete getmobil run today::22.10.2026: no run started",
    ]
