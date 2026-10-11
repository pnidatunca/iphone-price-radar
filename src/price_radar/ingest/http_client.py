"""Polite HTTP client shared by every source adapter.

The crawling policy is enforced here, so an adapter cannot bypass it:

* one client per site, strictly sequential requests;
* a random gap (default 2.0-3.5 s) between two requests;
* robots.txt is honoured, including groups addressed to the crawler's product token: a disallowed URL raises
  ``RobotsDisallowed`` without sending a request;
* a per-run request budget;
* 429 and 5xx responses are retried once, honouring ``Retry-After`` up to a limit;
* a bot challenge, a 401/403, a ``Retry-After`` above the limit, an unavailable robots.txt or too many consecutive
  errors stop the source for the rest of the run.
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
DEFAULT_RETRY_WAIT = 10.0


class CrawlStopped(Exception):
    """The source must not be requested again in this run."""


class BudgetExhausted(CrawlStopped):
    """The per-run request budget is spent."""


class RobotsDisallowed(Exception):
    """robots.txt disallows the URL; no request was sent."""


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
            follow_redirects=True,
            transport=transport,
        )
        self._rules: list[Rule] | None = None
        self._last_request_at: float | None = None
        self._extra_wait = 0.0
        self._consecutive_errors = 0
        self.requests_made = 0
        self.stopped_reason: str | None = None

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def get(self, url: str) -> httpx.Response:
        """GET a URL on this site.

        Any status that is not a bot challenge is returned as is (callers decide what a 404 means).
        Raises RobotsDisallowed, FetchFailed or CrawlStopped (BudgetExhausted is a CrawlStopped).
        """
        self._ensure_running()
        if urlsplit(url).hostname != self.host:
            raise ValueError(f"{url} is not on {self.host}")
        if not self.allowed(url):
            raise RobotsDisallowed(url)
        return self._fetch(url)

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
            self._last_request_at = self._clock()
            try:
                response = self._http.get(url)
            except httpx.HTTPError as err:
                failure = f"{type(err).__name__} for {url}"
                self._extra_wait = DEFAULT_RETRY_WAIT
            else:
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
