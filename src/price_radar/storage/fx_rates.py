"""Exchange rates in Neon: ``ref.fx_rates``, kept permanently (plan section 9).

    python -m price_radar.storage.fx_rates load --file DIR/rates.jsonl
    python -m price_radar.storage.fx_rates known-dates --out FILE [--currency USD]

One row per currency and announcement day, as TCMB published it (``price_radar.fx.tcmb``). ``valid_from`` is the day
after the announcement and a rate applies until the next bulletin, so the rate of a snapshot day D is the row with the
latest ``valid_from`` on or before D (plan 5.4). A published bulletin does not change, so loading only inserts and a
rerun inserts nothing twice.

``known-dates`` lists the announcement days already stored for every requested currency, so the next run requests only
the bulletins it is missing.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

import psycopg

from price_radar.config import database_url
from price_radar.fx.tcmb import CURRENCIES, FxRate

log = logging.getLogger(__name__)

DDL = """
create schema if not exists ref;
create table if not exists ref.fx_rates (
    currency          text not null,
    announced_on      date not null,
    valid_from        date not null,
    bulletin_no       text,
    unit              integer not null,
    forex_buying      numeric,
    forex_selling     numeric not null,
    banknote_buying   numeric,
    banknote_selling  numeric,
    source_url        text not null,
    fetched_at        timestamptz not null,
    parser_version    text not null,
    loaded_at         timestamptz not null default now(),
    primary key (currency, announced_on)
);
"""
COLUMNS = (
    "currency",
    "announced_on",
    "valid_from",
    "bulletin_no",
    "unit",
    "forex_buying",
    "forex_selling",
    "banknote_buying",
    "banknote_selling",
    "source_url",
    "fetched_at",
    "parser_version",
)


def read_rates(path: Path) -> list[FxRate]:
    """The rates of one run file, validated again: the file is the only thing between the crawl and the database."""
    rates = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rates.append(FxRate.model_validate_json(line))
        except ValueError as err:
            raise ValueError(f"{path}:{number}: {err}") from err
    return rates


def rate_rows(rates: Iterable[FxRate]) -> list[tuple[Any, ...]]:
    return [tuple(getattr(rate, column) for column in COLUMNS) for rate in rates]


def load(conn: psycopg.Connection, rates: list[FxRate]) -> int:
    """Insert the rates in one transaction; rows already stored are left as they are. Returns the rows inserted."""
    placeholders = ", ".join(["%s"] * len(COLUMNS))
    with conn.transaction():
        conn.execute(DDL)
        with conn.cursor() as cur:
            cur.executemany(
                f"insert into ref.fx_rates ({', '.join(COLUMNS)}) values ({placeholders})"
                " on conflict (currency, announced_on) do nothing",
                rate_rows(rates),
            )
            return max(cur.rowcount, 0)


def known_dates(conn: psycopg.Connection, currencies: Iterable[str]) -> list[date]:
    """Announcement days stored for every one of ``currencies``; empty before the first load."""
    codes = sorted(set(currencies))
    if conn.execute("select to_regclass('ref.fx_rates')").fetchone()[0] is None:
        return []
    rows = conn.execute(
        "select announced_on from ref.fx_rates where currency = any(%s)"
        " group by announced_on having count(distinct currency) = %s order by announced_on",
        (codes, len(codes)),
    ).fetchall()
    return [day for (day,) in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m price_radar.storage.fx_rates")
    commands = parser.add_subparsers(dest="command", required=True)
    load_cmd = commands.add_parser("load", help="load the rates.jsonl of one run into ref.fx_rates")
    load_cmd.add_argument("--file", type=Path, required=True)
    dates_cmd = commands.add_parser("known-dates", help="write the announcement days already stored")
    dates_cmd.add_argument("--out", type=Path, required=True)
    dates_cmd.add_argument("--currency", action="append", help="currency (repeatable, default USD)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")

    url = database_url()
    if not url:
        print("NEON_DATABASE_URL is not set (environment or .env)", file=sys.stderr)
        return 2
    with psycopg.connect(url, connect_timeout=30) as conn:
        if args.command == "load":
            rates = read_rates(args.file)
            count = load(conn, rates)
            log.info("%s of %s rates inserted into ref.fx_rates", count, len(rates))
        else:
            days = known_dates(conn, [code.upper() for code in args.currency or CURRENCIES])
            args.out.write_text("".join(f"{day.isoformat()}\n" for day in days), encoding="utf-8")
            log.info("%s stored announcement days written to %s", len(days), args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
