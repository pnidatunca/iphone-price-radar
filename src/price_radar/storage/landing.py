"""Phase 0 landing zone in Neon (decision K19).

Trial runs load their offers here unchanged, one JSON document per offer, together with the run summary. Phase 1 will
import them into the real schema. Until then this database is the only place raw offers are kept: nothing is uploaded
as a GitHub artifact, because artifacts of a public repository are public.

    python -m price_radar.storage.landing load --run-dir DIR --run-id ID [--exit-code N]
    python -m price_radar.storage.landing known-ids --out FILE

``known-ids`` lists the Easycep ids whose unit page was read in an earlier run (their offers carry a SKU), so the next
run reads unit pages only for new ids.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from price_radar.config import database_url

log = logging.getLogger(__name__)

DDL = """
create schema if not exists raw;
create table if not exists raw.landing_runs (
    run_id      text primary key,
    source      text not null,
    started_at  timestamptz not null,
    finished_at timestamptz not null,
    exit_code   integer,
    summary     jsonb not null,
    loaded_at   timestamptz not null default now()
);
create table if not exists raw.landing (
    run_id        text not null references raw.landing_runs (run_id),
    offer_key     text not null,
    source        text not null,
    snapshot_date date not null,
    fetched_at    timestamptz not null,
    offer         jsonb not null,
    primary key (run_id, offer_key)
);
create index if not exists landing_source_date on raw.landing (source, snapshot_date);
"""


@dataclass(frozen=True)
class LandingRun:
    """One run directory as written by ``price_radar.ingest.run``."""

    run_id: str
    source: str
    summary: dict[str, Any]
    offers: list[dict[str, Any]]


def read_run(run_dir: Path, run_id: str) -> LandingRun:
    """Read run_summary.json and offers.jsonl. A run that crashed or ran with --summary-only has no offers file."""
    summary = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))
    source = summary["source"]
    offers: list[dict[str, Any]] = []
    offers_path = run_dir / "offers.jsonl"
    if offers_path.exists():
        for number, line in enumerate(offers_path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            offer = json.loads(line)
            if offer.get("platform") != source:
                raise ValueError(f"{offers_path}:{number}: a {offer.get('platform')!r} offer in a {source} run")
            offers.append(offer)
    return LandingRun(run_id=run_id, source=source, summary=summary, offers=offers)


def offer_rows(run: LandingRun) -> list[tuple[Any, ...]]:
    return [
        (run.run_id, offer["offer_key"], run.source, offer["snapshot_date"], offer["fetched_at"], Jsonb(offer))
        for offer in run.offers
    ]


def load(conn: psycopg.Connection, run: LandingRun, exit_code: int | None) -> int:
    """Insert one run and its offers in a single transaction. Returns the number of offers inserted; a run that is
    already loaded is left untouched and 0 is returned, so a retried job never duplicates data."""
    with conn.transaction():
        conn.execute(DDL)
        inserted = conn.execute(
            "insert into raw.landing_runs (run_id, source, started_at, finished_at, exit_code, summary)"
            " values (%s, %s, %s, %s, %s, %s) on conflict (run_id) do nothing",
            (
                run.run_id,
                run.source,
                run.summary["started_at"],
                run.summary["finished_at"],
                exit_code,
                Jsonb(run.summary),
            ),
        ).rowcount
        if not inserted:
            log.warning("run %s is already loaded; nothing inserted", run.run_id)
            return 0
        with conn.cursor() as cur:
            cur.executemany(
                "insert into raw.landing (run_id, offer_key, source, snapshot_date, fetched_at, offer)"
                " values (%s, %s, %s, %s, %s, %s)",
                offer_rows(run),
            )
    return len(run.offers)


def known_unit_ids(conn: psycopg.Connection) -> list[str]:
    """Easycep ids whose unit page was read in an earlier run; empty before the first load."""
    if conn.execute("select to_regclass('raw.landing')").fetchone()[0] is None:
        return []
    rows = conn.execute(
        "select distinct offer_key from raw.landing"
        " where source = 'easycep' and offer->>'sku' is not null order by offer_key"
    ).fetchall()
    return [key.removeprefix("easycep:") for (key,) in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m price_radar.storage.landing")
    commands = parser.add_subparsers(dest="command", required=True)
    load_cmd = commands.add_parser("load", help="load one run directory into raw.landing")
    load_cmd.add_argument("--run-dir", type=Path, required=True)
    load_cmd.add_argument("--run-id", required=True)
    load_cmd.add_argument("--exit-code", type=int)
    ids_cmd = commands.add_parser("known-ids", help="write the Easycep ids whose unit page was read before")
    ids_cmd.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")

    url = database_url()
    if not url:
        print("NEON_DATABASE_URL is not set (environment or .env)", file=sys.stderr)
        return 2
    with psycopg.connect(url, connect_timeout=30) as conn:
        if args.command == "load":
            run = read_run(args.run_dir, args.run_id)
            count = load(conn, run, args.exit_code)
            log.info("loaded run %s: %s %s offers", run.run_id, count, run.source)
        else:
            ids = known_unit_ids(conn)
            args.out.write_text("".join(f"{item}\n" for item in ids), encoding="utf-8")
            log.info("%s known Easycep ids written to %s", len(ids), args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
