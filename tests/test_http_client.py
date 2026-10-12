import random

import httpx
import pytest

from price_radar.ingest.http_client import (
    BudgetExhausted,
    CrawlPolicy,
    CrawlStopped,
    FetchFailed,
    PoliteClient,
    RedirectRefused,
    RobotsDisallowed,
)

BASE = "https://shop.example"
# Like Getmobil's file: a query parameter needs one rule for "?param=" and one for "&param=".
ROBOTS = httpx.Response(200, text="User-agent: *\nDisallow: /ara\nDisallow: /*?minPrice=*\nDisallow: /*&minPrice=*\n")


class FakeTime:
    """A clock that only moves when the client sleeps, so waits can be asserted exactly."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(routes, latency=0.0, **policy):
    """routes maps "path?query" to a response, or to a list of responses consumed in order.
    latency is how many (fake) seconds every response takes."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.raw_path.decode()
        calls.append((key, request.headers["User-Agent"]))
        fake.now += latency
        value = routes[key]
        return value.pop(0) if isinstance(value, list) else value

    fake = FakeTime()
    client = PoliteClient(
        BASE,
        CrawlPolicy(user_agent="TestBot/1.0 (+https://example.org)", **policy),
        transport=httpx.MockTransport(handler),
        sleep=fake.sleep,
        clock=fake.clock,
        rng=random.Random(0),
    )
    return client, calls, fake


def paths(calls):
    return [path for path, _ in calls]


def test_disallowed_url_is_never_requested():
    client, calls, _ = make_client({"/robots.txt": ROBOTS})
    with pytest.raises(RobotsDisallowed):
        client.get(f"{BASE}/ara?term=iphone")
    with pytest.raises(RobotsDisallowed):
        client.get(f"{BASE}/list?a=1&x=2&minPrice=5")
    assert paths(calls) == ["/robots.txt"]


def test_robots_group_for_our_bot_is_honoured():
    robots = httpx.Response(200, text="User-agent: *\nAllow: /\n\nUser-agent: TestBot\nDisallow: /\n")
    client, calls, _ = make_client({"/robots.txt": robots})
    with pytest.raises(RobotsDisallowed):
        client.get(f"{BASE}/a")
    assert paths(calls) == ["/robots.txt"]


def redirect(location: str, status: int = 301) -> httpx.Response:
    return httpx.Response(status, headers={"Location": location})


def test_redirect_hop_is_a_spaced_request_of_its_own():
    client, calls, fake = make_client(
        {"/robots.txt": ROBOTS, "/old": redirect("/new/"), "/new/": httpx.Response(200, text="ok")}, latency=7.0
    )
    response = client.get(f"{BASE}/old")
    assert response.status_code == 200
    assert response.url.path == "/new/"
    assert [r.status_code for r in response.history] == [301]
    assert paths(calls) == ["/robots.txt", "/old", "/new/"]
    assert client.requests_made == 3
    assert len(fake.sleeps) == 2
    assert all(2.0 <= s <= 3.5 for s in fake.sleeps)


def test_redirect_to_a_disallowed_url_is_not_requested():
    client, calls, _ = make_client({"/robots.txt": ROBOTS, "/old": redirect("/ara?term=x", 302)})
    with pytest.raises(RobotsDisallowed):
        client.get(f"{BASE}/old")
    assert paths(calls) == ["/robots.txt", "/old"]


def test_redirect_to_another_site_is_refused():
    client, calls, _ = make_client({"/robots.txt": ROBOTS, "/old": redirect("https://other.example/x")})
    with pytest.raises(RedirectRefused):
        client.get(f"{BASE}/old")
    assert paths(calls) == ["/robots.txt", "/old"]


def test_redirect_chains_are_limited():
    routes = {"/robots.txt": ROBOTS, **{f"/r{i}": redirect(f"/r{i + 1}") for i in range(5)}}
    client, calls, _ = make_client(routes)
    with pytest.raises(RedirectRefused):
        client.get(f"{BASE}/r0")
    assert paths(calls) == ["/robots.txt", "/r0", "/r1", "/r2", "/r3"]


def test_redirect_hops_count_against_the_budget():
    client, calls, _ = make_client({"/robots.txt": ROBOTS, "/old": redirect("/new")}, max_requests=2)
    with pytest.raises(BudgetExhausted):
        client.get(f"{BASE}/old")
    assert paths(calls) == ["/robots.txt", "/old"]


def test_requests_are_spaced_and_identified():
    client, calls, fake = make_client({"/robots.txt": ROBOTS, "/a": httpx.Response(200), "/b": httpx.Response(200)})
    client.get(f"{BASE}/a")
    client.get(f"{BASE}/b")
    assert paths(calls) == ["/robots.txt", "/a", "/b"]
    assert len(fake.sleeps) == 2
    assert all(2.0 <= s <= 3.5 for s in fake.sleeps)
    assert {ua for _, ua in calls} == {"TestBot/1.0 (+https://example.org)"}


def test_gap_starts_after_the_response_ends():
    client, _, fake = make_client(
        {"/robots.txt": ROBOTS, "/a": httpx.Response(200), "/b": httpx.Response(200)}, latency=7.0
    )
    client.get(f"{BASE}/a")
    client.get(f"{BASE}/b")
    # Every response takes 7 s, yet each new request still waits 2-3.5 s after the previous response finished.
    assert len(fake.sleeps) == 2
    assert all(2.0 <= s <= 3.5 for s in fake.sleeps)


def test_response_durations_are_recorded():
    client, _, _ = make_client({"/robots.txt": ROBOTS, "/a": httpx.Response(200)}, latency=7.0)
    client.get(f"{BASE}/a")
    # robots.txt and the page each took 7 (fake) seconds; the waits in between are not part of a response.
    assert client.response_seconds == [7.0, 7.0]


def test_failed_requests_have_no_response_duration():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return ROBOTS
        raise httpx.ConnectError("unreachable", request=request)

    fake = FakeTime()
    client = PoliteClient(
        BASE,
        CrawlPolicy(user_agent="TestBot/1.0"),
        transport=httpx.MockTransport(handler),
        sleep=fake.sleep,
        clock=fake.clock,
        rng=random.Random(0),
    )
    with pytest.raises(FetchFailed):
        client.get(f"{BASE}/a")
    assert len(client.response_seconds) == 1  # robots.txt only


def test_budget_includes_robots_and_stops_the_source():
    client, calls, _ = make_client({"/robots.txt": ROBOTS, "/a": httpx.Response(200)}, max_requests=2)
    client.get(f"{BASE}/a")
    with pytest.raises(BudgetExhausted):
        client.get(f"{BASE}/a")
    with pytest.raises(CrawlStopped):
        client.get(f"{BASE}/a")
    assert len(calls) == 2


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403),
        httpx.Response(200, text="<html><title>Just a moment...</title></html>"),
        httpx.Response(200, text='<html><title>Challenge Validation</title><div id="bm-verify"></div></html>'),
    ],
)
def test_bot_challenge_stops_the_source(response):
    client, calls, _ = make_client({"/robots.txt": ROBOTS, "/a": response, "/b": httpx.Response(200)})
    with pytest.raises(CrawlStopped):
        client.get(f"{BASE}/a")
    with pytest.raises(CrawlStopped):
        client.get(f"{BASE}/b")
    assert paths(calls) == ["/robots.txt", "/a"]


def test_retry_after_is_honoured():
    client, calls, fake = make_client(
        {"/robots.txt": ROBOTS, "/a": [httpx.Response(429, headers={"Retry-After": "5"}), httpx.Response(200)]}
    )
    assert client.get(f"{BASE}/a").status_code == 200
    assert paths(calls) == ["/robots.txt", "/a", "/a"]
    assert fake.sleeps[-1] >= 5 + 2.0


def test_long_retry_after_stops_the_source_instead_of_waiting():
    client, _, fake = make_client({"/robots.txt": ROBOTS, "/a": httpx.Response(429, headers={"Retry-After": "3600"})})
    with pytest.raises(CrawlStopped):
        client.get(f"{BASE}/a")
    assert max(fake.sleeps) < 60


def test_server_error_is_retried_once_then_fails():
    client, calls, _ = make_client({"/robots.txt": ROBOTS, "/a": [httpx.Response(503), httpx.Response(503)]})
    with pytest.raises(FetchFailed):
        client.get(f"{BASE}/a")
    assert paths(calls) == ["/robots.txt", "/a", "/a"]


def test_consecutive_errors_stop_the_source():
    client, _, _ = make_client(
        {"/robots.txt": ROBOTS, "/a": [httpx.Response(503)] * 2, "/b": [httpx.Response(503)] * 2},
        max_consecutive_errors=3,
    )
    with pytest.raises(FetchFailed):
        client.get(f"{BASE}/a")
    with pytest.raises(CrawlStopped):
        client.get(f"{BASE}/b")


def test_success_resets_the_error_count():
    client, _, _ = make_client(
        {
            "/robots.txt": ROBOTS,
            "/a": [httpx.Response(503), httpx.Response(200)],
            "/b": [httpx.Response(503), httpx.Response(200)],
        },
        max_consecutive_errors=2,
    )
    assert client.get(f"{BASE}/a").status_code == 200
    assert client.get(f"{BASE}/b").status_code == 200


def test_not_found_is_returned_to_the_caller():
    client, _, _ = make_client({"/robots.txt": ROBOTS, "/gone": httpx.Response(404)})
    assert client.get(f"{BASE}/gone").status_code == 404


def test_missing_robots_allows_everything():
    client, _, _ = make_client({"/robots.txt": httpx.Response(404), "/ara": httpx.Response(200)})
    assert client.get(f"{BASE}/ara").status_code == 200


def test_unavailable_robots_stops_the_source():
    client, calls, _ = make_client({"/robots.txt": [httpx.Response(503), httpx.Response(503)]})
    with pytest.raises(CrawlStopped):
        client.get(f"{BASE}/a")
    assert paths(calls) == ["/robots.txt", "/robots.txt"]


def test_other_hosts_are_rejected():
    client, calls, _ = make_client({})
    with pytest.raises(ValueError):
        client.get("https://other.example/a")
    assert calls == []
