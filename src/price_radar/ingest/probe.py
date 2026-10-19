"""Source probe: can this crawler read a candidate site, and would it keep being able to?

    python -m price_radar.ingest.probe [--label github] [--annotate] URL [URL ...]

Trendyol answered every request from the owner's computer and blocked the first one from GitHub Actions (19.10.2026):
a site has to be checked from where the crawler runs, not from where it is developed. This tool requests a few pages
per site through the polite client (honest User-Agent, robots.txt, the usual gap, at most ``MAX_REQUESTS_PER_HOST``
requests) and records for each URL:

* whether robots.txt allows it, and the HTTP status, redirects, size and time of the answer;
* a bot challenge or access denial, with the answer's headers even then;
* signs of a bot-management service (Cloudflare, Akamai, DataDome, Imperva, PerimeterX, AWS WAF): such sites may let a
  few requests through and block a daily crawl, so they need a volume test before anyone builds on them;
* what the page carries: JSON-LD types, Next.js data, the number of iPhone mentions and of TL prices.

Output: one JSON line per URL on stdout; with ``--annotate`` also a GitHub Actions notice per URL, since annotations
can be read through the API where job logs cannot. No page content is stored.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

import httpx

from price_radar.config import load_settings
from price_radar.ingest.http_client import (
    CrawlPolicy,
    CrawlStopped,
    FetchFailed,
    PoliteClient,
    RedirectRefused,
    RobotsDisallowed,
)

MAX_REQUESTS_PER_HOST = 10
JSON_LD = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)
TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
TL_PRICE = re.compile(r"\d{1,3}(?:\.\d{3})+(?:,\d{2})?\s*(?:TL|₺)|₺\s*\d{1,3}(?:\.\d{3})+")


class _Recorder(httpx.BaseTransport):
    """Keeps the last answer of the wrapped transport, so its headers are known even when the client stops on it."""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self.inner = inner
        self.last: httpx.Response | None = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.last = self.inner.handle_request(request)
        return self.last

    def close(self) -> None:
        self.inner.close()


def bot_management(headers: httpx.Headers) -> list[str]:
    """Services in front of the site, named by the answer's headers and Set-Cookie names. A CDN alone ("cloudflare",
    "akamai") only serves pages; a bot-management product ("... bot management", "akamai bot manager", "datadome",
    "imperva", "perimeterx", "aws waf") scores clients and may block a daily crawl that a few requests got through."""
    names = {name.lower() for name in headers}
    server = headers.get("server", "").lower()
    cookies = {part.split("=", 1)[0].strip().lower() for part in headers.get_list("set-cookie")}

    def cookie(*prefixes: str) -> bool:
        return any(name.startswith(prefix) for name in cookies for prefix in prefixes)

    found = []
    if "cf-ray" in names or server == "cloudflare":
        found.append("cloudflare")
    if "cf-mitigated" in names or cookie("__cf_bm", "cf_clearance"):
        found.append("cloudflare bot management")
    if server.startswith("akamai") or names & {"akamai-grn", "x-akamai-transformed", "akamai-cache-status"}:
        found.append("akamai")
    if cookie("_abck", "bm_sz", "ak_bmsc", "bm_sv"):
        found.append("akamai bot manager")
    if names & {"x-datadome", "x-dd-b"} or cookie("datadome"):
        found.append("datadome")
    if "x-iinfo" in names or headers.get("x-cdn", "").lower() == "incapsula" or cookie("incap_ses_", "visid_incap_"):
        found.append("imperva")
    if "x-px" in names or cookie("_px"):
        found.append("perimeterx")
    if "x-amzn-waf-action" in names or cookie("aws-waf-token"):
        found.append("aws waf")
    return found


def page_markers(html: str) -> dict[str, Any]:
    """What a page carries that a parser could use. Counts only: no content is kept."""
    types: set[str] = set()
    stock: Counter[str] = Counter()  # schema.org availability of the offers, e.g. InStock, OutOfStock
    for block in JSON_LD.findall(html):
        try:
            data = json.loads(block.strip())
        except ValueError:
            types.add("(invalid)")
            continue
        for node in _ld_nodes(data):
            kind = node.get("@type")
            types.update(k for k in (kind if isinstance(kind, list) else [kind]) if isinstance(k, str))
            if isinstance(node.get("availability"), str):
                stock[node["availability"].rsplit("/", 1)[-1]] += 1
    title = TITLE.search(html)
    return {
        "title": " ".join(title.group(1).split())[:80] if title else None,
        "json_ld": sorted(types),
        "offer_availability": dict(sorted(stock.items())),
        "next_data": "__NEXT_DATA__" in html,
        "next_flight": "self.__next_f.push" in html,
        "iphone_mentions": len(re.findall(r"iphone", html, re.I)),
        "tl_prices": len(TL_PRICE.findall(html)),
    }


def _ld_nodes(node: Any) -> Iterable[dict[str, Any]]:
    """Every JSON-LD object, following the keys that hold products and offers."""
    if isinstance(node, list):
        for item in node:
            yield from _ld_nodes(item)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "itemListElement", "offers", "item", "mainEntity"):
            if key in node:
                yield from _ld_nodes(node[key])


def probe(
    urls: list[str],
    policy: CrawlPolicy,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Probe the URLs host by host, in the given order; one result per URL."""
    by_host: dict[str, list[str]] = {}
    for url in urls:
        parts = urlsplit(url)
        by_host.setdefault(f"{parts.scheme}://{parts.netloc}", []).append(url)
    policy = replace(policy, max_requests=MAX_REQUESTS_PER_HOST)
    results = []
    for base, host_urls in by_host.items():
        recorder = _Recorder(transport or httpx.HTTPTransport())
        with PoliteClient(base, policy, transport=recorder, sleep=sleep) as client:
            for url in host_urls:
                results.append(_probe_one(client, recorder, url))
    return results


def _probe_one(client: PoliteClient, recorder: _Recorder, url: str) -> dict[str, Any]:
    result: dict[str, Any] = {"url": url, "outcome": "ok"}
    started = time.monotonic()
    response = None
    try:
        response = client.get(url)
    except RobotsDisallowed:
        result["outcome"] = "disallowed by robots.txt"
    except RedirectRefused as err:
        result["outcome"] = f"redirect refused: {err}"
    except FetchFailed as err:
        result["outcome"] = f"failed: {err}"
    except CrawlStopped as err:
        result["outcome"] = f"stopped: {err}"
    result["seconds"] = round(time.monotonic() - started, 2)
    answer = response or recorder.last
    if answer is not None:
        result["status"] = answer.status_code
        result["bot_management"] = bot_management(answer.headers)
        result["server"] = answer.headers.get("server")
    if response is not None:
        result["final_url"] = str(response.url)
        result["redirects"] = len(response.history)
        result["bytes"] = len(response.content)
        result.update(page_markers(response.text))
    return result


def annotation(result: dict[str, Any], label: str) -> str:
    host = urlsplit(result["url"]).netloc
    parts = [result["outcome"], f"HTTP {result.get('status', '-')}"]
    if result.get("bot_management"):
        parts.append("bot management: " + "+".join(result["bot_management"]))
    if "bytes" in result:
        parts.append(f"{result['bytes']} B in {result['seconds']} s")
        parts.append("JSON-LD " + (",".join(result["json_ld"]) or "none"))
        if result["offer_availability"]:
            parts.append("offers " + ", ".join(f"{state} x{n}" for state, n in result["offer_availability"].items()))
        if result["next_data"] or result["next_flight"]:
            parts.append("Next.js data")
        parts.append(f"iPhone x{result['iphone_mentions']}, TL prices x{result['tl_prices']}")
    path = urlsplit(result["url"]).path or "/"
    return f"::notice title=probe {label} {host}{path[:40]}::" + "; ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m price_radar.ingest.probe", description=__doc__.splitlines()[0])
    parser.add_argument("urls", nargs="+", help="https URLs; robots.txt of each host is read first")
    parser.add_argument("--label", default="local", help="where the probe runs, e.g. github")
    parser.add_argument("--annotate", action="store_true", help="also print a GitHub Actions notice per URL")
    args = parser.parse_args(argv)
    if any(urlsplit(url).scheme != "https" for url in args.urls):
        parser.error("only https URLs are probed")
    for result in probe(args.urls, load_settings().crawler.policy()):
        result["label"] = args.label
        print(json.dumps(result, ensure_ascii=False))
        if args.annotate:
            print(annotation(result, args.label))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
