"""Plumbing shared by the source adapters: the context a crawl runs in and the statistics it records.

An adapter is a generator ``crawl(context) -> Iterator[RawOffer]``. It requests pages only through
``CrawlContext.get``, which goes through the polite client and turns a robots.txt refusal or a failed request into a
recorded statistic instead of an exception, so one bad page never ends the run. ``CrawlStopped`` (budget spent, bot
challenge, too many errors) still propagates: the source is finished for this run.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any, Protocol

import httpx

from price_radar.config import ScopeSettings
from price_radar.ingest.http_client import FetchFailed, RedirectRefused, RobotsDisallowed

log = logging.getLogger(__name__)

# Turkey has used UTC+3 all year since 2016; a fixed offset avoids depending on the tz database on Windows.
TURKEY = timezone(timedelta(hours=3), "TRT")
MAX_EXAMPLES = 50  # refusals, failures and warnings kept per run; the counts stay exact


class Client(Protocol):
    def get(self, url: str) -> httpx.Response: ...

    def allowed(self, url: str) -> bool: ...


@dataclass
class CrawlStats:
    """What one source run did. Counts are exact; the lists keep the first ``MAX_EXAMPLES`` entries."""

    source: str
    responses: Counter[str] = field(default_factory=Counter)
    status_codes: Counter[int] = field(default_factory=Counter)
    redirects: int = 0
    offers: int = 0
    in_stock_offers: int = 0
    sellers_per_variant: Counter[int] = field(default_factory=Counter)
    refusal_count: int = 0
    refusals: list[dict[str, str]] = field(default_factory=list)
    failure_count: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)
    warning_count: int = 0
    warnings: list[str] = field(default_factory=list)
    counters: Counter[str] = field(default_factory=Counter)
    observed: dict[str, Counter[str]] = field(default_factory=dict)  # values seen in the run, e.g. warranty types
    discovery: dict[str, Any] = field(default_factory=dict)
    stop_reason: str | None = None

    def refuse(self, kind: str, url: str, reason: str) -> None:
        self.refusal_count += 1
        if len(self.refusals) < MAX_EXAMPLES:
            self.refusals.append({"kind": kind, "url": url, "reason": reason})
        log.warning("refused %s %s: %s", kind, url, reason)

    def fail(self, kind: str, url: str, error: str) -> None:
        self.failure_count += 1
        if len(self.failures) < MAX_EXAMPLES:
            self.failures.append({"kind": kind, "url": url, "error": error})
        log.warning("failed %s %s: %s", kind, url, error)

    def observe(self, name: str, value: str) -> None:
        """Count one occurrence of ``value`` under ``name``; the run summary lists every distinct value."""
        self.observed.setdefault(name, Counter())[value] += 1

    def warn(self, message: str) -> None:
        self.warning_count += 1
        if len(self.warnings) < MAX_EXAMPLES:
            self.warnings.append(message)
        log.warning("%s", message)


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class CrawlContext:
    """Everything an adapter needs for one run. ``snapshot_date`` is the Turkish calendar day the run started on."""

    client: Client
    scope: ScopeSettings
    stats: CrawlStats
    max_variants: int | None = None
    known_ids: frozenset[str] = frozenset()
    now: Callable[[], datetime] = utc_now
    snapshot_date: date = field(init=False)

    def __post_init__(self) -> None:
        self.snapshot_date = self.now().astimezone(TURKEY).date()

    def get(self, url: str, kind: str) -> httpx.Response | None:
        """GET through the polite client. Returns None (and records why) when robots.txt refuses the URL or a redirect
        target, a redirect leaves the site, or the request fails after its retry. The client checks every redirect hop
        before requesting it, so a refused target is never fetched."""
        try:
            response = self.client.get(url)
        except RobotsDisallowed as err:
            self.stats.refuse(kind, str(err) or url, "robots.txt")
            return None
        except RedirectRefused as err:
            self.stats.refuse(kind, url, str(err))
            return None
        except FetchFailed as err:
            self.stats.fail(kind, url, str(err))
            return None
        self.stats.responses[kind] += 1
        self.stats.status_codes[response.status_code] += 1
        if response.history:
            self.stats.redirects += 1
        return response
