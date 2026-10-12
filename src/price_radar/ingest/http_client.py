"""Polite HTTP client shared by every source adapter.

The crawling policy is enforced here, so an adapter cannot bypass it:

* one client per site, strictly sequential requests;
* a random gap (default 2.0-3.5 s) between the end of one response and the next request;
* robots.txt is honoured, including groups addressed to the crawler's product token: a disallowed URL raises
  ``RobotsDisallowed`` without sending a request, and so does a redirect to a disallowed URL;
* redirects are followed one policed hop at a time and never to another site;
* a per-run request budget;
* 429 and 5xx responses are retried once, honouring ``Retry-After`` up to a limit;
* a bot challenge, a 401/403, a ``Retry-After`` above the limit, an unavailable robots.txt or too many consecutive
  errors stop the source for the rest of the run.

The duration of every response is kept in ``response_seconds`` for the run summary.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn
from urllib.parse import urlsplit

import httpx

from price_radar.ingest.robots import Rule, is_allowed, parse_robots

CHALLENGE_MARKERS = (
    "Just a moment...",
    "Challenge Validation",
    "bm-verify",
    "_cf_chl_opt",
    "Attention Required! | Cloudflare",
)
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 3
DEFAULT_RETRY_WAIT = 10.0


class CrawlStopped(Exception):
    """The source must not be requested again in this run."""


class BudgetExhausted(CrawlStopped):
    """The per-run request budget is spent."""


class RobotsDisallowed(Exception):
    """robots.txt disallows the URL; no request was sent."""


class RedirectRefused(Exception):
    """A redirect pointed to another site or there were too many hops; the target was not requested."""


class FetchFailed(Exception):
    """The request still failed after its retry (network error, 429 or 5xx)."""


@dataclass(frozen=True)
class CrawlPolicy:
    user_agent: str
    delay_seconds: tuple[float, float] = (2.0, 3.5)
    max_requests: int = 1500
    max_consecutive_errors: int = 10
    max_retry_after_seconds: float = 60.0
    timeout_seconds: float = 25.0


def is_challenge(response: httpx.Response) -> bool:
    """True for responses that mean "you are being treated as a bot"."""
    if response.status_code in (401, 403):
        return True
    head = response.content[:20_000].decode("utf-8", "replace")
    return any(marker in head for marker in CHALLENGE_MARKERS)


def retry_after_seconds(response: httpx.Response) -> float:
    value = response.headers.get("Retry-After", "").strip()
    return float(value) if value.isdigit() else DEFAULT_RETRY_WAIT


class PoliteClient:
    """Sequential, rate-limited HTTP client for a single site."""

    def __init__(
        self,
        base_url: str,
        policy: CrawlPolicy,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.host = urlsplit(self.base_url).hostname
        self.policy = policy
        self.product_token = policy.user_agent.split("/", 1)[0].strip()
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random()
        self._http = httpx.Client(
            headers={"User-Agent": policy.user_agent, "Accept-Language": "tr-TR,tr;q=0.9"},
            timeout=policy.timeout_seconds,
            follow_redirects=False,  # redirects are followed in get(), one policed request per hop
            transport=transport,
        )
        self._rules: list[Rule] | None = None
        self._last_request_at: float | None = None
        self._extra_wait = 0.0
        self._consecutive_errors = 0
        self.requests_made = 0
        self.response_seconds: list[float] = []  # one entry per response received, retries and robots.txt included
        self.stopped_reason: str | None = None

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def get(self, url: str) -> httpx.Response:
        """GET a URL on this site.

        Any status that is not a bot challenge is returned as is (callers decide what a 404 means). Redirects are
        followed here rather than by httpx, so every hop gets the gap, counts against the budget and is checked against
        robots.txt before it is requested; the returned response carries the earlier hops in ``history``.
        Raises RobotsDisallowed, RedirectRefused, FetchFailed or CrawlStopped (BudgetExhausted is a CrawlStopped).
        """
        self._ensure_running()
        if urlsplit(url).hostname != self.host:
            raise ValueError(f"{url} is not on {self.host}")
        if not self.allowed(url):
            raise RobotsDisallowed(url)
        history: list[httpx.Response] = []
        while True:
            response = self._fetch(url)
            location = response.headers.get("Location")
            if response.status_code not in REDIRECT_STATUS or not location:
                response.history = history
                return response
            history.append(response)
            target = str(response.url.join(location))
            if len(history) > MAX_REDIRECTS:
                raise RedirectRefused(f"more than {MAX_REDIRECTS} redirects, last target {target}")
            if urlsplit(target).hostname != self.host:
                raise RedirectRefused(f"{url} redirects to another site: {target}")
            if not self.allowed(target):
                raise RobotsDisallowed(target)
            url = target

    def allowed(self, url: str) -> bool:
        """robots.txt decision for a URL on this site; robots.txt is fetched once per client."""
        parts = urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        return is_allowed(self._robots(), path)

    def _robots(self) -> list[Rule]:
        if self._rules is None:
            try:
                response = self._fetch(f"{self.base_url}/robots.txt")
            except FetchFailed as err:
                self._stop(f"robots.txt unavailable: {err}")
            if response.status_code == 200:
                self._rules = parse_robots(response.text, self.product_token)
            elif response.status_code in (404, 410):
                self._rules = []
            else:
                self._stop(f"robots.txt returned HTTP {response.status_code}")
        return self._rules

    def _fetch(self, url: str) -> httpx.Response:
        failure = ""
        for _attempt in range(2):
            self._ensure_running()
            if self.requests_made >= self.policy.max_requests:
                self.stopped_reason = f"request budget of {self.policy.max_requests} spent"
                raise BudgetExhausted(self.stopped_reason)
            self._wait_turn()
            self.requests_made += 1
            response: httpx.Response | None = None
            started = self._clock()
            try:
                response = self._http.get(url)
            except httpx.HTTPError as err:
                failure = f"{type(err).__name__} for {url}"
                self._extra_wait = DEFAULT_RETRY_WAIT
            # The gap is measured from the end of the response, so a slow server never gets back-to-back requests.
            self._last_request_at = self._clock()
            if response is not None:
                self.response_seconds.append(self._last_request_at - started)
                if is_challenge(response):
                    self._stop(f"bot challenge or HTTP {response.status_code} at {url}")
                if response.status_code not in RETRYABLE_STATUS:
                    self._consecutive_errors = 0
                    return response
                failure = f"HTTP {response.status_code} for {url}"
                wait = retry_after_seconds(response)
                if wait > self.policy.max_retry_after_seconds:
                    self._stop(f"{failure} with Retry-After {wait:.0f} s")
                self._extra_wait = wait
            self._record_error(failure)
        raise FetchFailed(failure)

    def _wait_turn(self) -> None:
        if self._last_request_at is None:
            return
        gap = self._rng.uniform(*self.policy.delay_seconds) + self._extra_wait
        self._extra_wait = 0.0
        remaining = self._last_request_at + gap - self._clock()
        if remaining > 0:
            self._sleep(remaining)

    def _record_error(self, failure: str) -> None:
        self._consecutive_errors += 1
        if self._consecutive_errors >= self.policy.max_consecutive_errors:
            self._stop(f"{self._consecutive_errors} consecutive errors, last: {failure}")

    def _ensure_running(self) -> None:
        if self.stopped_reason is not None:
            raise CrawlStopped(self.stopped_reason)

    def _stop(self, reason: str) -> NoReturn:
        self.stopped_reason = reason
        raise CrawlStopped(reason)
