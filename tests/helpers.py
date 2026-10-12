"""Shared test helpers: the synthetic fixture pages and a fake site behind httpx.MockTransport.

The pages under tests/fixtures/ keep the structure of real Getmobil and Easycep pages (flight rows, React element
arrays, reference paths, text rows) but every value in them is invented: ids, prices, stock, seller ids and names,
SKUs, EANs and URLs. No test makes a network request.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from price_radar.config import load_settings
from price_radar.ingest.crawl import CrawlContext, CrawlStats
from price_radar.ingest.http_client import CrawlPolicy, PoliteClient

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 12, 0, 17, tzinfo=UTC)  # 03:17 in Turkey, when the nightly run starts
ROBOTS = "User-agent: *\nDisallow: /ara\nDisallow: /*?orderby*\n"


def page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeSite:
    """Routes "path?query" to a page (str), a response, or a list of them served in order (the last one repeats).

    Unknown paths answer 404. Every requested path is recorded, robots.txt included.
    """

    def __init__(self, base_url: str, routes: dict[str, Any]) -> None:
        self.base_url = base_url
        self.routes = {"/robots.txt": httpx.Response(200, text=ROBOTS), **routes}
        self.requested: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        key = request.url.raw_path.decode()
        self.requested.append(key)
        value = self.routes.get(key, httpx.Response(404, text="Sayfa Bulunamadı"))
        if isinstance(value, list):
            value = value.pop(0) if len(value) > 1 else value[0]
        return httpx.Response(200, text=value) if isinstance(value, str) else value

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def client(self, max_requests: int = 1500) -> PoliteClient:
        return PoliteClient(
            self.base_url,
            CrawlPolicy(user_agent="TestBot/1.0 (+https://example.org)", max_requests=max_requests),
            transport=self.transport(),
            sleep=lambda seconds: None,
            rng=random.Random(0),
        )

    def context(self, *, max_requests: int = 1500, **options: Any) -> CrawlContext:
        return CrawlContext(
            client=self.client(max_requests),
            scope=load_settings().scope,
            stats=CrawlStats("test"),
            now=lambda: NOW,
            **options,
        )
