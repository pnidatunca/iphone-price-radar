"""Command line entry point: one collection run of one source.

    python -m price_radar.ingest.run --source getmobil --out data/runs/getmobil [--max-requests 40]

Writes ``DIR/offers.jsonl`` (one RawOffer per line) and ``DIR/run_summary.json``; raw HTML is never written. With
``--summary-only`` no offers are written and the summary is also printed to stdout as Markdown, ready to be appended to
a GitHub job summary. The summary holds counts, timings, error reasons and the values seen in a few vocabulary fields
(``observed``: Trendyol's warranty types), no catalogue content, because job summaries of a public repository are
public.

Exit codes: 0 when the run ended normally (a spent request budget included), 1 when the site stopped the run (bot
challenge, 401/403, too many errors, robots.txt unavailable) or the run crashed, 2 for a usage error. The summary is
written in every case but a usage error.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import math
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from price_radar.config import load_settings
from price_radar.ingest.crawl import CrawlContext, CrawlStats, utc_now
from price_radar.ingest.http_client import BudgetExhausted, CrawlStopped, PoliteClient
from price_radar.ingest.sources import easycep, getmobil, trendyol

log = logging.getLogger(__name__)

SOURCES = {"getmobil": getmobil, "easycep": easycep, "trendyol": trendyol}
MARKDOWN_EXAMPLES = 5


class UsageError(Exception):
    """Bad command line input that argparse cannot catch (for example a malformed --known-ids file)."""


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m price_radar.ingest.run", description="Collect offers from one source."
    )
    parser.add_argument("--source", required=True, choices=sorted(SOURCES))
    parser.add_argument("--max-requests", type=_positive_int, help="request budget for this run (default: settings)")
    parser.add_argument(
        "--max-variants",
        type=_positive_int,
        help="at most this many variant pages (Getmobil), unit pages (Easycep) or product pages (Trendyol)",
    )
    parser.add_argument(
        "--known-ids",
        type=Path,
        help="Easycep only: ids whose unit page was read before, one per line ('easycep:' prefix allowed)",
    )
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    parser.add_argument("--summary-only", action="store_true", help="write no offers; print the summary as Markdown")
    parser.add_argument("--markdown", action="store_true", help="also print the summary as Markdown (counts only)")
    args = parser.parse_args(argv)
    if args.known_ids is not None and args.source != "easycep":
        parser.error("--known-ids applies to --source easycep only")
    return args


def main(
    argv: list[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = utc_now,
) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request would drown the warnings
    settings = load_settings()
    source = settings.sources.get(args.source)
    if source is None or not source.enabled:
        print(f"source {args.source} is not enabled in config/settings.yaml", file=sys.stderr)
        return 2
    try:
        known_ids = read_known_ids(args.known_ids) if args.known_ids else frozenset()
    except (OSError, UsageError) as err:
        print(f"cannot use --known-ids: {err}", file=sys.stderr)
        return 2
    policy = settings.crawler.policy()
    if args.max_requests:
        policy = dataclasses.replace(policy, max_requests=args.max_requests)
    args.out.mkdir(parents=True, exist_ok=True)

    stats = CrawlStats(args.source)
    started_at, started = now(), time.monotonic()
    failed = False
    with ExitStack() as stack:
        client = stack.enter_context(PoliteClient(source.base_url, policy, transport=transport, sleep=sleep))
        offers_file = None if args.summary_only else stack.enter_context(_open(args.out / "offers.jsonl"))
        ctx = CrawlContext(
            client=client,
            scope=settings.scope,
            stats=stats,
            max_variants=args.max_variants,
            known_ids=known_ids,
            now=now,
        )
        seen: set[str] = set()
        try:
            for offer in SOURCES[args.source].crawl(ctx):
                if offer.offer_key in seen:
                    stats.counters["duplicate_offers"] += 1
                    continue
                seen.add(offer.offer_key)
                stats.offers += 1
                stats.in_stock_offers += offer.in_stock
                if offers_file is not None:
                    offers_file.write(offer.model_dump_json() + "\n")
        except BudgetExhausted as err:
            stats.stop_reason = str(err)
        except CrawlStopped as err:
            stats.stop_reason = str(err)
            failed = True
        except Exception as err:  # a bug must not lose the summary of what was collected so far
            log.exception("%s run crashed", args.source)
            stats.stop_reason = f"crashed: {type(err).__name__}: {err}"
            failed = True
        summary = build_summary(
            args=args,
            stats=stats,
            client=client,
            snapshot_date=ctx.snapshot_date.isoformat(),
            started_at=started_at,
            finished_at=now(),
            runtime_seconds=time.monotonic() - started,
        )
    (args.out / "run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    log.info(
        "%s: %s offers (%s in stock), stop reason: %s",
        args.source,
        stats.offers,
        stats.in_stock_offers,
        summary["stop_reason"],
    )
    if args.summary_only or args.markdown:
        sys.stdout.write(summary_markdown(summary, "Access test" if args.summary_only else "Collection run"))
    return 1 if failed else 0


def read_known_ids(path: Path) -> frozenset[str]:
    """Ids from a text file, one per line; blank lines and '#' comments are ignored."""
    ids = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        token = line.split("#", 1)[0].strip().removeprefix("easycep:")
        if not token:
            continue
        if not token.isascii() or not token.isdigit():
            raise UsageError(f"{path}:{number}: {token!r} is not a numeric id")
        ids.add(token)
    return frozenset(ids)


def build_summary(
    *,
    args: argparse.Namespace,
    stats: CrawlStats,
    client: PoliteClient,
    snapshot_date: str,
    started_at: datetime,
    finished_at: datetime,
    runtime_seconds: float,
) -> dict[str, Any]:
    durations = sorted(client.response_seconds)
    return {
        "source": stats.source,
        "parser_version": SOURCES[stats.source].PARSER_VERSION,
        "user_agent": client.policy.user_agent,
        "snapshot_date": snapshot_date,
        "started_at": started_at.astimezone(UTC).isoformat(),
        "finished_at": finished_at.astimezone(UTC).isoformat(),
        "runtime_seconds": round(runtime_seconds, 1),
        "stop_reason": stats.stop_reason or "completed",
        "requests": {"made": client.requests_made, "budget": client.policy.max_requests},
        "pages_fetched": dict(sorted(stats.responses.items())),
        "status_codes": {str(code): count for code, count in sorted(stats.status_codes.items())},
        "redirects": stats.redirects,
        "offers": stats.offers,
        "in_stock_offers": stats.in_stock_offers,
        "sellers_per_variant": {str(n): count for n, count in sorted(stats.sellers_per_variant.items())},
        "refusals": {"count": stats.refusal_count, "examples": stats.refusals},
        "failures": {"count": stats.failure_count, "examples": stats.failures},
        "warnings": {"count": stats.warning_count, "examples": stats.warnings},
        "counters": dict(sorted(stats.counters.items())),
        "observed": {name: dict(values.most_common()) for name, values in sorted(stats.observed.items())},
        "discovery": stats.discovery,
        "response_seconds": {
            "count": len(durations),
            "p50": _round(percentile(durations, 0.5)),
            "p90": _round(percentile(durations, 0.9)),
            "max": _round(durations[-1] if durations else None),
        },
        "options": {
            "max_requests": args.max_requests,
            "max_variants": args.max_variants,
            "known_ids": str(args.known_ids) if args.known_ids else None,
            "summary_only": args.summary_only,
            "markdown": args.markdown,
        },
    }


def percentile(sorted_values: list[float], share: float) -> float | None:
    """Nearest-rank percentile of an ascending list; None for an empty list."""
    if not sorted_values:
        return None
    rank = max(1, math.ceil(share * len(sorted_values)))
    return sorted_values[rank - 1]


def summary_markdown(summary: dict[str, Any], title: str = "Access test") -> str:
    """The run summary as a short Markdown section with counts only."""
    times = summary["response_seconds"]
    timing = (
        f"p50 {times['p50']} s, p90 {times['p90']} s, max {times['max']} s (n={times['count']})"
        if times["count"]
        else "no responses"
    )
    rows = [
        ("Stop reason", summary["stop_reason"]),
        ("User-Agent", f"`{summary['user_agent']}`"),
        ("Requests", f"{summary['requests']['made']} of {summary['requests']['budget']}"),
        ("Pages fetched", _pairs(summary["pages_fetched"]) or "none"),
        ("HTTP status", _pairs(summary["status_codes"]) or "none"),
        ("Redirects", summary["redirects"]),
        ("Offers", f"{summary['offers']} ({summary['in_stock_offers']} in stock)"),
        ("Sellers per variant", _pairs(summary["sellers_per_variant"]) or "none"),
        ("Refusals", summary["refusals"]["count"]),
        ("Failures", summary["failures"]["count"]),
        ("Warnings", summary["warnings"]["count"]),
        ("Response time", timing),
        ("Runtime", _duration(summary["runtime_seconds"])),
    ]
    if summary["counters"]:
        rows.append(("Counters", _pairs(summary["counters"])))
    lines = [f"### {title}: {summary['source']}", "", "| | |", "|---|---|"]
    lines += [f"| {name} | {_cell(value)} |" for name, value in rows]
    for title, key, field in (("Failures", "failures", "error"), ("Refusals", "refusals", "reason")):
        examples = summary[key]["examples"][:MARKDOWN_EXAMPLES]
        if examples:
            lines += ["", f"{title} (first {len(examples)}):", ""]
            lines += [f"- {example['kind']}: {_cell(example[field])}" for example in examples]
    return "\n".join(lines) + "\n\n"


def _open(path: Path):
    return path.open("w", encoding="utf-8", newline="\n")


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _pairs(mapping: dict[str, Any]) -> str:
    return ", ".join(f"{key}: {value}" for key, value in mapping.items())


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _duration(seconds: float) -> str:
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes} min {rest} s" if minutes else f"{rest} s"


if __name__ == "__main__":
    raise SystemExit(main())
