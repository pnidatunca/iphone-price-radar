"""TCMB exchange rates: the official USD/TRY rate that converts prices to dollars (plan section 5.4).

    python -m price_radar.fx.tcmb --out DIR [--days 10 | --from 2026-07-01] [--to 2026-10-13] [--known-dates FILE]

Source. The Central Bank of the Republic of Turkey publishes its indicative rates every business day at 15:30 as an XML
bulletin, which stays online under the day it was announced: ``/kurlar/YYYYMM/DDMMYYYY.xml``. Weekends and public
holidays have no bulletin (404). Only these archive files are requested, never ``today.xml``: the file name pins the
announcement day, so a late run cannot store the wrong day, and a run catches up on every bulletin of the last days that
is not stored yet (``--known-dates``), which also covers a missed night.

Date rule. A rate applies from the day after its announcement until the next bulletin, so the rate of a snapshot day D
is the last one announced before D (``rate_for``): Friday's bulletin serves Saturday, Sunday and Monday. Prices are
converted with the forex selling rate; the other three rates are kept as published.

Writes ``DIR/rates.jsonl`` (one FxRate per currency and bulletin) and ``DIR/run_summary.json``. Exit codes as in
``price_radar.ingest.run``: 0 for a normal run, 1 when the site stopped it or it crashed, 2 for a usage error.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from price_radar.config import ScopeSettings, load_settings
from price_radar.ingest import urls
from price_radar.ingest.crawl import TURKEY, CrawlContext, CrawlStats, utc_now
from price_radar.ingest.http_client import BudgetExhausted, CrawlStopped, PoliteClient
from price_radar.ingest.normalize import as_decimal, as_int, clean_text
from price_radar.ingest.run import UsageError, percentile
from price_radar.ingest.schemas import error_summary

log = logging.getLogger(__name__)

SOURCE = "tcmb"
PARSER_VERSION = "tcmb-1"
HOST = "www.tcmb.gov.tr"
BASE_URL = f"https://{HOST}"
URL_FORMS = (urls.UrlForm("bulletin", re.compile(r"/kurlar/20[0-9]{4}/[0-9]{8}\.xml")),)
CURRENCIES = ("USD",)
# Days checked by a nightly run: enough to catch up after the longest closure (Kurban Bayramı can be nine days).
DEFAULT_DAYS = 10
MAX_RATE = Decimal("100000")  # sanity bound per unit: a larger value is a parsing error


class FxRate(BaseModel):
    """One currency of one TCMB bulletin as published: Turkish lira per ``unit`` of the currency."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["tcmb"] = SOURCE
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    announced_on: date
    valid_from: date  # the day after the announcement
    bulletin_no: str | None = None
    unit: int = Field(gt=0)
    forex_buying: Decimal | None = Field(default=None, gt=0, le=MAX_RATE)
    forex_selling: Decimal = Field(gt=0, le=MAX_RATE)
    banknote_buying: Decimal | None = Field(default=None, gt=0, le=MAX_RATE)
    banknote_selling: Decimal | None = Field(default=None, gt=0, le=MAX_RATE)
    source_url: str
    fetched_at: datetime
    parser_version: str = PARSER_VERSION

    @model_validator(mode="after")
    def _consistent(self) -> FxRate:
        if self.valid_from != self.announced_on + timedelta(days=1):
            raise ValueError("valid_from must be the day after announced_on")
        if self.forex_buying is not None and self.forex_buying > self.forex_selling:
            raise ValueError(f"forex buying {self.forex_buying} is above forex selling {self.forex_selling}")
        if self.fetched_at.tzinfo is None:
            raise ValueError("fetched_at must carry a time zone")
        return self


class BulletinError(ValueError):
    """The document is not a usable TCMB bulletin of the requested day."""


@dataclass(frozen=True)
class Bulletin:
    announced_on: date
    bulletin_no: str | None
    rates: tuple[FxRate, ...]
    missing: tuple[str, ...]  # requested currencies the bulletin does not list


def archive_url(day: date) -> str:
    return f"{BASE_URL}/kurlar/{day:%Y%m}/{day:%d%m%Y}.xml"


def parse_bulletin(
    content: bytes,
    url: str,
    *,
    expected: date,
    fetched_at: datetime,
    currencies: Iterable[str] = CURRENCIES,
) -> Bulletin:
    """The requested currencies of one bulletin. Raises BulletinError when the document is not a bulletin, is the
    bulletin of another day, or a requested currency has no valid forex selling rate. The standard library parser
    does not resolve external entities."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as err:
        raise BulletinError(f"not an XML bulletin: {err}") from err
    if root.tag != "Tarih_Date":
        raise BulletinError(f"root element is {root.tag!r}, not 'Tarih_Date'")
    announced = _date(root.get("Tarih"), "%d.%m.%Y")
    if announced is None or _date(root.get("Date"), "%m/%d/%Y") != announced:
        raise BulletinError(f"inconsistent bulletin dates: Tarih={root.get('Tarih')!r}, Date={root.get('Date')!r}")
    if announced != expected:
        raise BulletinError(f"the bulletin of {announced} was served for {expected}")
    bulletin_no = clean_text(root.get("Bulten_No")) or None
    wanted = list(dict.fromkeys(currencies))
    elements: dict[str, ET.Element] = {}
    for element in root.iter("Currency"):
        code = element.get("CurrencyCode") or element.get("Kod")
        if code in wanted:
            elements.setdefault(code, element)
    rates = []
    for code, element in elements.items():
        try:
            rates.append(
                FxRate(
                    currency=code,
                    announced_on=announced,
                    valid_from=announced + timedelta(days=1),
                    bulletin_no=bulletin_no,
                    unit=as_int(clean_text(element.findtext("Unit"))),
                    forex_buying=as_decimal(element.findtext("ForexBuying")),
                    forex_selling=as_decimal(element.findtext("ForexSelling")),
                    banknote_buying=as_decimal(element.findtext("BanknoteBuying")),
                    banknote_selling=as_decimal(element.findtext("BanknoteSelling")),
                    source_url=url,
                    fetched_at=fetched_at,
                )
            )
        except ValidationError as err:
            raise BulletinError(f"{code}: {error_summary(err)}") from err
    return Bulletin(announced, bulletin_no, tuple(rates), tuple(code for code in wanted if code not in elements))


def rate_for(rates: Iterable[FxRate], snapshot_date: date, currency: str = "USD") -> FxRate | None:
    """The rate that applies on ``snapshot_date``: the last one announced before that day (plan 5.4)."""
    earlier = [rate for rate in rates if rate.currency == currency and rate.announced_on < snapshot_date]
    return max(earlier, key=lambda rate: rate.announced_on, default=None)


def bulletin_days(first: date, last: date, known: frozenset[date] = frozenset()) -> list[date]:
    """Weekdays from ``first`` to ``last`` (inclusive) whose bulletin is not stored yet. Weekends never have a bulletin
    and are not requested; a public holiday is requested and answers 404."""
    days = []
    day = first
    while day <= last:
        if day.weekday() < 5 and day not in known:
            days.append(day)
        day += timedelta(days=1)
    return days


def collect(ctx: CrawlContext, days: Iterable[date], currencies: Iterable[str] = CURRENCIES) -> Iterator[FxRate]:
    """Request the bulletin of each day and yield its rates. A 404 is a day without a bulletin (a public holiday, or a
    bulletin not out yet); anything else that is not a bulletin is recorded as a failure and retried next run."""
    currencies = tuple(currencies)
    for day in days:
        url = archive_url(day)
        urls.check(url, HOST, URL_FORMS)
        response = ctx.get(url, "bulletin")
        if response is None:
            continue
        if response.status_code == 404:
            ctx.stats.counters["days_without_bulletin"] += 1
            ctx.stats.discovery.setdefault("days_without_bulletin", []).append(day.isoformat())
            continue
        if response.status_code != 200:
            ctx.stats.fail("bulletin", url, f"HTTP {response.status_code}")
            continue
        try:
            bulletin = parse_bulletin(response.content, url, expected=day, fetched_at=ctx.now(), currencies=currencies)
        except BulletinError as err:
            ctx.stats.fail("bulletin", url, str(err))
            continue
        ctx.stats.counters["bulletins"] += 1
        for code in bulletin.missing:
            ctx.stats.warn(f"bulletin {bulletin.bulletin_no} of {day} has no {code} rate")
        for rate in bulletin.rates:
            ctx.stats.counters["rates"] += 1
            yield rate


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m price_radar.fx.tcmb", description="Collect TCMB exchange rates.")
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    span = parser.add_mutually_exclusive_group()
    span.add_argument("--days", type=_positive_int, help=f"check the last N days up to --to (default {DEFAULT_DAYS})")
    span.add_argument("--from", dest="first", type=_iso_date, help="first announcement day to check (YYYY-MM-DD)")
    parser.add_argument(
        "--to", dest="last", type=_iso_date, help="last announcement day (default: yesterday in Turkey)"
    )
    parser.add_argument("--known-dates", type=Path, help="announcement days already stored, one YYYY-MM-DD per line")
    parser.add_argument(
        "--currency", action="append", type=_currency, help="currency to keep (repeatable; default USD)"
    )
    parser.add_argument("--max-requests", type=_positive_int, help="request budget for this run (default: settings)")
    parser.add_argument("--markdown", action="store_true", help="also print the summary as Markdown")
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = utc_now,
) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    yesterday = now().astimezone(TURKEY).date() - timedelta(days=1)
    last = args.last or yesterday
    first = args.first or last - timedelta(days=(args.days or DEFAULT_DAYS) - 1)
    if last > yesterday:
        print(f"--to {last} is not before today: a bulletin is complete only after its day", file=sys.stderr)
        return 2
    if first > last:
        print(f"--from {first} is after --to {last}", file=sys.stderr)
        return 2
    settings = load_settings()
    source = settings.sources.get(SOURCE)
    if source is None or not source.enabled:
        print(f"source {SOURCE} is not enabled in config/settings.yaml", file=sys.stderr)
        return 2
    try:
        known = read_known_dates(args.known_dates) if args.known_dates else frozenset()
    except (OSError, UsageError) as err:
        print(f"cannot use --known-dates: {err}", file=sys.stderr)
        return 2
    policy = settings.crawler.policy()
    if args.max_requests:
        policy = replace(policy, max_requests=args.max_requests)
    days = bulletin_days(first, last, known)
    args.out.mkdir(parents=True, exist_ok=True)

    stats = CrawlStats(SOURCE)
    rates: list[FxRate] = []
    started_at, started = now(), time.monotonic()
    failed = False
    with (
        PoliteClient(source.base_url, policy, transport=transport, sleep=sleep) as client,
        (args.out / "rates.jsonl").open("w", encoding="utf-8", newline="\n") as rates_file,
    ):
        ctx = CrawlContext(client=client, scope=ScopeSettings(), stats=stats, now=now)
        try:
            for rate in collect(ctx, days, args.currency or CURRENCIES):
                rates.append(rate)
                rates_file.write(rate.model_dump_json() + "\n")
        except BudgetExhausted as err:
            stats.stop_reason = str(err)
        except CrawlStopped as err:
            stats.stop_reason = str(err)
            failed = True
        except Exception as err:  # a bug must not lose the summary of what was collected so far
            log.exception("%s run crashed", SOURCE)
            stats.stop_reason = f"crashed: {type(err).__name__}: {err}"
            failed = True
        summary = build_summary(
            stats=stats,
            client=client,
            first=first,
            last=last,
            to_check=len(days),
            already_stored=len(bulletin_days(first, last)) - len(days),
            rates=rates,
            started_at=started_at,
            finished_at=now(),
            runtime_seconds=time.monotonic() - started,
        )
    (args.out / "run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    log.info(
        "%s: %s bulletins, %s rates, stop reason: %s", SOURCE, summary["bulletins"], len(rates), summary["stop_reason"]
    )
    if args.markdown:
        sys.stdout.write(summary_markdown(summary))
    return 1 if failed else 0


def read_known_dates(path: Path) -> frozenset[date]:
    """Dates from a text file, one YYYY-MM-DD per line; blank lines and '#' comments are ignored."""
    days = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        token = line.split("#", 1)[0].strip()
        if not token:
            continue
        day = _date(token, "%Y-%m-%d")
        if day is None:
            raise UsageError(f"{path}:{number}: {token!r} is not a YYYY-MM-DD date")
        days.add(day)
    return frozenset(days)


def build_summary(
    *,
    stats: CrawlStats,
    client: PoliteClient,
    first: date,
    last: date,
    to_check: int,
    already_stored: int,
    rates: list[FxRate],
    started_at: datetime,
    finished_at: datetime,
    runtime_seconds: float,
) -> dict[str, Any]:
    durations = sorted(client.response_seconds)
    latest = max(rates, key=lambda rate: rate.announced_on, default=None)
    return {
        "source": SOURCE,
        "parser_version": PARSER_VERSION,
        "user_agent": client.policy.user_agent,
        "started_at": started_at.astimezone(UTC).isoformat(),
        "finished_at": finished_at.astimezone(UTC).isoformat(),
        "runtime_seconds": round(runtime_seconds, 1),
        "stop_reason": stats.stop_reason or "completed",
        "requests": {"made": client.requests_made, "budget": client.policy.max_requests},
        "status_codes": {str(code): count for code, count in sorted(stats.status_codes.items())},
        "days": {
            "from": first.isoformat(),
            "to": last.isoformat(),
            "to_check": to_check,
            "already_stored": already_stored,
        },
        "bulletins": stats.counters["bulletins"],
        "rates": len(rates),
        "days_without_bulletin": stats.discovery.get("days_without_bulletin", []),
        "latest": (
            {
                "announced_on": latest.announced_on.isoformat(),
                "currency": latest.currency,
                "forex_selling": str(latest.forex_selling),
            }
            if latest
            else None
        ),
        "refusals": {"count": stats.refusal_count, "examples": stats.refusals},
        "failures": {"count": stats.failure_count, "examples": stats.failures},
        "warnings": {"count": stats.warning_count, "examples": stats.warnings},
        "response_seconds": {
            "count": len(durations),
            "p50": _round(percentile(durations, 0.5)),
            "max": _round(durations[-1] if durations else None),
        },
    }


def summary_markdown(summary: dict[str, Any]) -> str:
    """The run summary as a short Markdown section. TCMB rates are public, so the latest one is shown."""
    days = summary["days"]
    latest = summary["latest"]
    rows = [
        ("Stop reason", summary["stop_reason"]),
        (
            "Days",
            f"{days['from']} to {days['to']}: {days['to_check']} to check, {days['already_stored']} stored before",
        ),
        ("Requests", f"{summary['requests']['made']} of {summary['requests']['budget']}"),
        ("HTTP status", ", ".join(f"{code}: {n}" for code, n in summary["status_codes"].items()) or "none"),
        ("Bulletins", summary["bulletins"]),
        ("Days without bulletin", ", ".join(summary["days_without_bulletin"]) or "none"),
        ("Rates", summary["rates"]),
        (
            "Latest",
            f"{latest['currency']} {latest['forex_selling']}, announced {latest['announced_on']}" if latest else "none",
        ),
        ("Failures", summary["failures"]["count"]),
        ("Refusals", summary["refusals"]["count"]),
        ("Warnings", summary["warnings"]["count"]),
    ]
    lines = [f"### Exchange rates: {summary['source']}", "", "| | |", "|---|---|"]
    lines += [f"| {name} | {_cell(value)} |" for name, value in rows]
    examples = summary["failures"]["examples"][:5]
    if examples:
        lines += ["", f"Failures (first {len(examples)}):", ""]
        lines += [f"- {_cell(example['url'])}: {_cell(example['error'])}" for example in examples]
    return "\n".join(lines) + "\n\n"


def _date(text: str | None, pattern: str) -> date | None:
    try:
        return datetime.strptime((text or "").strip(), pattern).date()
    except ValueError:
        return None


def _iso_date(text: str) -> date:
    day = _date(text, "%Y-%m-%d")
    if day is None:
        raise argparse.ArgumentTypeError("must be a date as YYYY-MM-DD")
    return day


def _currency(text: str) -> str:
    code = text.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise argparse.ArgumentTypeError("must be a three-letter currency code")
    return code


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    raise SystemExit(main())
