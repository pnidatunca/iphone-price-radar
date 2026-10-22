"""Pipeline health checks (plan 7.5 and 12.5), read from ops.pipeline_runs and ref.fx_rates.

    python -m price_radar.monitoring.health --source getmobil [--context after-run] [--annotate] [--notify always]

The daily pipeline runs this after each run; the health-check workflow runs it again in the afternoon, because a run
that never started sends no failure e-mail and leaves nothing to look at. Errors: no complete run today or yesterday,
offers below half of the 7-day average, listing pages that call the crawler a bot. Warnings: the request budget
almost spent, many rejected offers, stale TCMB rates. The exit code is 1 when there is an error, so the job turns red
and GitHub mails its owner; ``--annotate`` writes GitHub notices, ``--notify`` sends a Telegram message.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from statistics import mean
from typing import Literal

import psycopg

from price_radar.config import database_url
from price_radar.ingest.crawl import TURKEY
from price_radar.notify import telegram

log = logging.getLogger(__name__)

BASELINE_DAYS = 7
DROP_SHARE = 0.5  # offers below this share of the 7-day average mean the site probably changed (plan 7.5)
BUDGET_SHARE = 0.9
REJECTED_SHARE = 0.05
FX_MAX_AGE_DAYS = 4  # a Friday bulletin is still the current one on Tuesday morning; a long holiday may warn

RUNS = """
select snapshot_date, started_at, exit_code, stop_reason, offers, requests,
       (summary->'requests'->>'budget')::int, rejected, warnings, failures,
       coalesce((summary->'counters'->>'listing_pages_is_bot_true')::int, 0)
from ops.pipeline_runs
where source = %s and snapshot_date >= %s::date - %s::int and snapshot_date <= %s::date
order by started_at
"""


@dataclass(frozen=True)
class RunRecord:
    snapshot_date: date
    started_at: datetime
    exit_code: int | None
    stop_reason: str
    offers: int
    requests: int
    budget: int | None
    rejected: int
    warnings: int
    failures: int
    bot_pages: int  # listing pages that said isBot=true

    @property
    def complete(self) -> bool:
        return self.exit_code == 0 and self.stop_reason == "completed"


@dataclass(frozen=True)
class Finding:
    level: Literal["error", "warning"]
    title: str
    message: str


def evaluate(
    runs: list[RunRecord], source: str, today: date, *, fx_latest: date | None = None
) -> tuple[list[Finding], str]:
    """The findings for ``today`` and a one-line summary of the day."""
    complete: dict[date, RunRecord] = {}
    for run in runs:
        if run.complete and run.offers >= getattr(complete.get(run.snapshot_date), "offers", -1):
            complete[run.snapshot_date] = run
    findings: list[Finding] = []
    current = complete.get(today)
    if current is None:
        attempts = [run for run in runs if run.snapshot_date == today]
        detail = (
            f"last attempt stopped: {attempts[-1].stop_reason} (exit {attempts[-1].exit_code})"
            if attempts
            else "no run started"
        )
        findings.append(Finding("error", f"No complete {source} run today", f"{today:%d.%m.%Y}: {detail}"))
    yesterday = today - timedelta(days=1)
    if yesterday not in complete and any(run.snapshot_date < yesterday for run in runs):
        findings.append(Finding("error", f"No {source} run yesterday", f"{yesterday:%d.%m.%Y} has no complete run"))
    baseline = [complete[day].offers for day in complete if today - timedelta(days=BASELINE_DAYS) <= day < today]
    average = mean(baseline) if baseline else None
    if current is not None:
        if average and current.offers < DROP_SHARE * average:
            findings.append(
                Finding(
                    "error",
                    f"{source} offers dropped",
                    f"{current.offers} offers against a {BASELINE_DAYS}-day average of {average:.0f}:"
                    " the site may have changed",
                )
            )
        if current.bot_pages:
            findings.append(
                Finding("error", f"{source} flags the crawler", f"{current.bot_pages} listing pages said isBot=true")
            )
        if current.budget and current.requests >= BUDGET_SHARE * current.budget:
            findings.append(
                Finding(
                    "warning",
                    f"{source} request budget nearly spent",
                    f"{current.requests} of {current.budget} requests",
                )
            )
        if current.offers and current.rejected > REJECTED_SHARE * current.offers:
            findings.append(
                Finding("warning", f"{source} rejected offers", f"{current.rejected} of {current.offers} offers")
            )
    if fx_latest is not None and (today - fx_latest).days > FX_MAX_AGE_DAYS:
        findings.append(Finding("warning", "TCMB rates are stale", f"newest bulletin: {fx_latest:%d.%m.%Y}"))
    if current is None:
        summary = f"{source} {today:%d.%m}: no complete run"
    else:
        average_text = f" (7-day average {average:,.0f})" if average else ""
        summary = (
            f"{source} {today:%d.%m}: {current.offers:,} offers{average_text}, {current.requests} requests,"
            f" warnings {current.warnings}, failures {current.failures}"
        )
    return findings, summary


def message(findings: list[Finding], summary: str, context: str) -> str:
    """The Telegram text: a status word, the summary, then one line per finding."""
    status = "ALERT" if any(f.level == "error" for f in findings) else "WARNING" if findings else "OK"
    lines = [f"[{status}] iPhone Price Radar, {context}", summary]
    lines += [f"- {finding.title}: {finding.message}" for finding in findings]
    return "\n".join(lines)


def annotations(findings: list[Finding], summary: str) -> list[str]:
    lines = [f"::notice title=Health::{summary}"]
    lines += [f"::{finding.level} title={finding.title}::{finding.message}" for finding in findings]
    return lines


def read_runs(conn: psycopg.Connection, source: str, today: date) -> list[RunRecord]:
    if conn.execute("select to_regclass('ops.pipeline_runs')").fetchone()[0] is None:
        return []
    rows = conn.execute(RUNS, (source, today, BASELINE_DAYS + 3, today)).fetchall()
    return [RunRecord(*row) for row in rows]


def latest_fx(conn: psycopg.Connection) -> date | None:
    if conn.execute("select to_regclass('ref.fx_rates')").fetchone()[0] is None:
        return None
    return conn.execute("select max(announced_on) from ref.fx_rates where currency = 'USD'").fetchone()[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m price_radar.monitoring.health")
    parser.add_argument("--source", required=True)
    parser.add_argument("--context", default="health check", help="where the check runs, shown in the message")
    parser.add_argument("--annotate", action="store_true", help="print GitHub Actions annotations")
    parser.add_argument(
        "--notify",
        choices=["always", "problems", "never"],
        default="problems",
        help="send a Telegram message always, only for findings, or never",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    url = database_url()
    if not url:
        print("NEON_DATABASE_URL is not set (environment or .env)", file=sys.stderr)
        return 2
    today = datetime.now(UTC).astimezone(TURKEY).date()
    with psycopg.connect(url, connect_timeout=30) as conn:
        runs = read_runs(conn, args.source, today)
        fx = latest_fx(conn)
    findings, summary = evaluate(runs, args.source, today, fx_latest=fx)
    print(message(findings, summary, args.context))
    if args.annotate:
        print("\n".join(annotations(findings, summary)))
    if args.notify == "always" or (args.notify == "problems" and findings):
        sent = telegram.send(message(findings, summary, args.context))
        log.info("Telegram message %s", "sent" if sent else "not sent")
    return 1 if any(finding.level == "error" for finding in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
