"""Raw offers and run health in Neon (plan section 8): raw.offer_snapshots, raw.rejected_offers, ops.pipeline_runs.

    python -m price_radar.storage.snapshots load --run-dir DIR --run-id ID --workflow NAME [--exit-code N]
    python -m price_radar.storage.snapshots loaded-today --source SOURCE
    python -m price_radar.storage.snapshots db-size [--warn-mb 350]
    python -m price_radar.storage.snapshots migrate-landing

One row per offer and day. ``payload`` is stored only when the offer's ``payload_hash`` differs from the hash of its
latest earlier row, so an offer that does not change costs one narrow row a day instead of its whole payload (plan
7.4); every row keeps its hash. A second run of the same day (the fallback run, a manual run) adds the offers the
first one missed and changes none. Every run, failed ones included, gets a row in ops.pipeline_runs, and a run that is
already there is not loaded again.

``migrate-landing`` copies the Phase 0 tables raw.landing and raw.landing_runs (decision K19) into these tables inside
the database. It never drops them.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from price_radar.config import database_url
from price_radar.ingest.crawl import TURKEY
from price_radar.ingest.schemas import RawOffer

log = logging.getLogger(__name__)

DDL = """
create schema if not exists raw;
create schema if not exists ops;
create table if not exists ops.pipeline_runs (
    run_id          text primary key,
    workflow        text not null,
    source          text not null,
    snapshot_date   date not null,
    started_at      timestamptz not null,
    finished_at     timestamptz not null,
    exit_code       integer,
    stop_reason     text not null,
    requests        integer not null,
    offers          integer not null,
    in_stock_offers integer not null,
    rejected        integer not null,
    warnings        integer not null,
    failures        integer not null,
    refusals        integer not null,
    runtime_seconds numeric not null,
    summary         jsonb not null,
    loaded_at       timestamptz not null default now()
);
create index if not exists pipeline_runs_source_date on ops.pipeline_runs (source, snapshot_date);
create table if not exists raw.offer_snapshots (
    snapshot_date   date not null,
    offer_key       text not null,
    platform        text not null,
    run_id          text not null references ops.pipeline_runs (run_id),
    fetched_at      timestamptz not null,
    model           text not null,
    storage_gb      integer not null,
    color           text not null,
    condition_raw   text not null,
    sim_type        text,
    battery_raw     text,
    warranty_raw    text,
    sku             text,
    condition_flags text[] not null,
    seller_id       bigint,
    is_buybox       boolean not null,
    price_tl        numeric not null,
    list_price_tl   numeric,
    promo_pct       numeric,
    stock_qty       integer,
    in_stock        boolean not null,
    product_url     text not null,
    offer_url       text not null,
    payload         jsonb,
    payload_hash    text not null,
    parser_version  text not null,
    primary key (snapshot_date, offer_key)
);
create index if not exists offer_snapshots_key_date on raw.offer_snapshots (offer_key, snapshot_date desc);
create table if not exists raw.rejected_offers (
    run_id        text not null references ops.pipeline_runs (run_id),
    snapshot_date date not null,
    platform      text not null,
    offer_key     text not null,
    reason        text not null
);
create index if not exists rejected_offers_date on raw.rejected_offers (snapshot_date);
"""
OFFER_COLUMNS = (
    "snapshot_date",
    "offer_key",
    "platform",
    "run_id",
    "fetched_at",
    "model",
    "storage_gb",
    "color",
    "condition_raw",
    "sim_type",
    "battery_raw",
    "warranty_raw",
    "sku",
    "condition_flags",
    "seller_id",
    "is_buybox",
    "price_tl",
    "list_price_tl",
    "promo_pct",
    "stock_qty",
    "in_stock",
    "product_url",
    "offer_url",
    "payload",
    "payload_hash",
    "parser_version",
)
RUN_COLUMNS = (
    "run_id",
    "workflow",
    "source",
    "snapshot_date",
    "started_at",
    "finished_at",
    "exit_code",
    "stop_reason",
    "requests",
    "offers",
    "in_stock_offers",
    "rejected",
    "warnings",
    "failures",
    "refusals",
    "runtime_seconds",
    "summary",
)
MIGRATE_RUNS = """
insert into ops.pipeline_runs (run_id, workflow, source, snapshot_date, started_at, finished_at, exit_code, stop_reason,
                               requests, offers, in_stock_offers, rejected, warnings, failures, refusals,
                               runtime_seconds, summary)
select run_id, 'trial-crawl', source, (summary->>'snapshot_date')::date, started_at, finished_at, exit_code,
       coalesce(summary->>'stop_reason', 'completed'), (summary->'requests'->>'made')::int,
       (summary->>'offers')::int, (summary->>'in_stock_offers')::int,
       coalesce((summary->'counters'->>'offers_rejected')::int, 0), (summary->'warnings'->>'count')::int,
       (summary->'failures'->>'count')::int, (summary->'refusals'->>'count')::int,
       (summary->>'runtime_seconds')::numeric, summary
from raw.landing_runs
on conflict (run_id) do nothing
"""
MIGRATE_OFFERS = """
insert into raw.offer_snapshots (snapshot_date, offer_key, platform, run_id, fetched_at, model, storage_gb, color,
                                 condition_raw, sim_type, battery_raw, warranty_raw, sku, condition_flags, seller_id,
                                 is_buybox, price_tl, list_price_tl, promo_pct, stock_qty, in_stock, product_url,
                                 offer_url, payload, payload_hash, parser_version)
select l.snapshot_date, l.offer_key, l.source, l.run_id, l.fetched_at, o->>'model', (o->>'storage_gb')::int,
       o->>'color', o->>'condition_raw', o->>'sim_type', o->>'battery_raw', o->>'warranty_raw', o->>'sku',
       array(select jsonb_array_elements_text(coalesce(o->'condition_flags', '[]'::jsonb))),
       (o->>'seller_id')::bigint, (o->>'is_buybox')::boolean, (o->>'price_tl')::numeric,
       (o->>'list_price_tl')::numeric, (o->>'promo_pct')::numeric, (o->>'stock_qty')::int,
       (o->>'in_stock')::boolean, o->>'product_url', o->>'offer_url',
       case when o->>'payload_hash' is distinct from lag(o->>'payload_hash')
                 over (partition by l.offer_key order by l.snapshot_date, l.fetched_at)
            then o->'payload' end,
       o->>'payload_hash', o->>'parser_version'
from raw.landing l
cross join lateral (select l.offer as o) d
order by l.snapshot_date, l.offer_key
on conflict (snapshot_date, offer_key) do nothing
"""
LAST_HASHES = """
select distinct on (offer_key) offer_key, payload_hash
from raw.offer_snapshots
where platform = %s and snapshot_date < %s
order by offer_key, snapshot_date desc
"""


@dataclass(frozen=True)
class Run:
    """One collection run: its summary, offers and rejected offers, as ``price_radar.ingest.run`` writes them."""

    run_id: str
    workflow: str
    source: str
    summary: dict[str, Any]
    exit_code: int | None
    offers: list[RawOffer]
    rejections: list[dict[str, str]]


def read_run_dir(run_dir: Path, run_id: str, workflow: str, exit_code: int | None) -> Run:
    """Read run_summary.json, offers.jsonl and rejected.jsonl. A run that crashed or ran with --summary-only may have
    neither of the last two."""
    summary = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))
    source = summary["source"]
    offers = [RawOffer.model_validate_json(line) for line in _lines(run_dir / "offers.jsonl")]
    for number, offer in enumerate(offers, start=1):
        if offer.platform != source:
            raise ValueError(f"offers.jsonl:{number}: a {offer.platform} offer in a {source} run")
    rejections = [json.loads(line) for line in _lines(run_dir / "rejected.jsonl")]
    return Run(run_id, workflow, source, summary, exit_code, offers, rejections)


def run_row(run: Run) -> tuple[Any, ...]:
    summary = run.summary
    return (
        run.run_id,
        run.workflow,
        run.source,
        summary["snapshot_date"],
        summary["started_at"],
        summary["finished_at"],
        run.exit_code,
        summary.get("stop_reason") or "completed",
        summary["requests"]["made"],
        summary["offers"],
        summary["in_stock_offers"],
        summary.get("counters", {}).get("offers_rejected", 0),
        summary["warnings"]["count"],
        summary["failures"]["count"],
        summary["refusals"]["count"],
        summary["runtime_seconds"],
        Jsonb(summary),
    )


def offer_rows(run: Run, last_hashes: dict[str, str]) -> list[tuple[Any, ...]]:
    """One row per offer; the payload is left out when the offer's hash equals its latest earlier one."""
    rows = []
    for offer in run.offers:
        record = offer.model_dump(mode="json")
        changed = last_hashes.get(offer.offer_key) != offer.payload_hash
        values = {
            **{column: getattr(offer, column, None) for column in OFFER_COLUMNS},
            "run_id": run.run_id,
            "condition_flags": list(offer.condition_flags),
            "payload": Jsonb(record["payload"]) if changed else None,
        }
        rows.append(tuple(values[column] for column in OFFER_COLUMNS))
    return rows


def load(conn: psycopg.Connection, run: Run) -> int | None:
    """Load one run in a single transaction. Returns the number of offer rows inserted, or None when the run is
    already loaded (a retried job never duplicates anything)."""
    columns, placeholders = ", ".join(OFFER_COLUMNS), ", ".join(["%s"] * len(OFFER_COLUMNS))
    with conn.transaction():
        conn.execute(DDL)
        inserted = conn.execute(
            f"insert into ops.pipeline_runs ({', '.join(RUN_COLUMNS)}) values ({', '.join(['%s'] * len(RUN_COLUMNS))})"
            " on conflict (run_id) do nothing",
            run_row(run),
        ).rowcount
        if not inserted:
            log.warning("run %s is already loaded; nothing inserted", run.run_id)
            return None
        snapshot_date = run.summary["snapshot_date"]
        last_hashes = dict(conn.execute(LAST_HASHES, (run.source, snapshot_date)).fetchall())
        with conn.cursor() as cur:
            cur.executemany(
                f"insert into raw.offer_snapshots ({columns}) values ({placeholders})"
                " on conflict (snapshot_date, offer_key) do nothing",
                offer_rows(run, last_hashes),
            )
            offers_inserted = max(cur.rowcount, 0)
            if run.rejections:
                cur.executemany(
                    "insert into raw.rejected_offers (run_id, snapshot_date, platform, offer_key, reason)"
                    " values (%s, %s, %s, %s, %s)",
                    [(run.run_id, snapshot_date, run.source, r["offer_key"], r["reason"]) for r in run.rejections],
                )
    return offers_inserted


def loaded_today(conn: psycopg.Connection, source: str, snapshot_date: str) -> bool:
    """True if a complete run of ``source`` for this Turkish calendar day is loaded: exit code 0 and the crawl got to
    the end. A run cut short by its request budget also exits 0, but it does not count as done."""
    if conn.execute("select to_regclass('ops.pipeline_runs')").fetchone()[0] is None:
        return False
    row = conn.execute(
        "select 1 from ops.pipeline_runs where source = %s and snapshot_date = %s and exit_code = 0"
        " and stop_reason = 'completed' limit 1",
        (source, snapshot_date),
    ).fetchone()
    return row is not None


def migrate_landing(conn: psycopg.Connection) -> tuple[int, int]:
    """Copy every Phase 0 run from raw.landing_runs and raw.landing inside the database, in one transaction, and
    return (runs, offer rows) inserted. The documents were validated as RawOffer when they were collected and keep
    the hash they were stored with; the payload is kept on the first day of an offer and whenever its hash differs
    from the day before, as in ``load``. Moving the data through the client would cost minutes on a slow network."""
    if conn.execute("select to_regclass('raw.landing_runs')").fetchone()[0] is None:
        return 0, 0
    with conn.transaction():
        conn.execute(DDL)
        runs = conn.execute(MIGRATE_RUNS).rowcount
        offers = conn.execute(MIGRATE_OFFERS).rowcount
    return runs, offers


def size_report(size_bytes: int, warn_mb: float, limit_mb: float) -> tuple[str, bool]:
    """A one-line Markdown report of the database size and whether it crossed the warning threshold."""
    size_mb = size_bytes / 1024 / 1024
    text = f"Neon database size: {size_mb:.1f} MB of the {limit_mb:.0f} MB free limit ({size_mb / limit_mb:.0%})"
    return text, size_mb >= warn_mb


def _lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m price_radar.storage.snapshots")
    commands = parser.add_subparsers(dest="command", required=True)
    load_cmd = commands.add_parser("load", help="load one run directory")
    load_cmd.add_argument("--run-dir", type=Path, required=True)
    load_cmd.add_argument("--run-id", required=True)
    load_cmd.add_argument("--workflow", required=True, help="name of the workflow that ran the crawl")
    load_cmd.add_argument("--exit-code", type=int)
    today_cmd = commands.add_parser("loaded-today", help="print 'true' if today's complete run of a source is loaded")
    today_cmd.add_argument("--source", required=True)
    size_cmd = commands.add_parser("db-size", help="report the database size; warn when it nears the free limit")
    size_cmd.add_argument("--warn-mb", type=float, default=350.0, help="warning threshold (default 70%% of 500 MB)")
    size_cmd.add_argument("--limit-mb", type=float, default=500.0, help="free storage limit (Neon, last known)")
    commands.add_parser("migrate-landing", help="copy the Phase 0 landing tables into these tables")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")

    url = database_url()
    if not url:
        print("NEON_DATABASE_URL is not set (environment or .env)", file=sys.stderr)
        return 2
    with psycopg.connect(url, connect_timeout=30) as conn:
        if args.command == "load":
            run = read_run_dir(args.run_dir, args.run_id, args.workflow, args.exit_code)
            count = load(conn, run)
            log.info("run %s: %s of %s %s offers inserted", run.run_id, count, len(run.offers), run.source)
        elif args.command == "loaded-today":
            today = datetime.now(UTC).astimezone(TURKEY).date().isoformat()
            print("true" if loaded_today(conn, args.source, today) else "false")
        elif args.command == "migrate-landing":
            runs, offers = migrate_landing(conn)
            log.info("%s runs and %s offer rows copied from raw.landing", runs, offers)
        else:
            size = conn.execute("select pg_database_size(current_database())").fetchone()[0]
            text, warn = size_report(size, args.warn_mb, args.limit_mb)
            print(text)
            if warn:
                # A GitHub Actions annotation: shown as a warning on the run page.
                print(f"::warning title=Neon storage::{text}. Phase 1 retention is needed soon.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
