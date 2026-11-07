"""The training set of the price model (plan 11.2-11.3): offer-price pairs built from core.fct_offer_daily.

    python -m price_radar.model.dataset extract [--out FILE] [--start DAY] [--end DAY]

The extract reads one row per in-stock offer and day, read-only, and writes it to a local Parquet file (default
data/model/offer_daily.parquet). The file holds numeric seller ids, which M2 needs for seller premiums, so it stays
on the machine or the runner that made it: data/ and *.parquet are never committed (K12), and no workflow uploads it.
The connection string comes from NEON_DATABASE_URL and is never printed.

A pair is one offer at one price, counted once however long it stays on sale (plan 11.3): an offer on sale for 60
days at the same price would otherwise weigh 60 times. A pair's features and its dollar price are those of the day it
was first seen; the dollar price uses that day's TCMB rate (plan 5.4).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_OUT = Path("data/model/offer_daily.parquet")
PLATFORM = "getmobil"  # the one platform collected (K3)

# What the extract reads. The offer's URL is not read; the seller id is (M2, plan 11.5).
COLUMNS = (
    "snapshot_date",
    "offer_key",
    "device_id",
    "model",
    "storage_gb",
    "generation",
    "series",
    "release_date",
    "device_age_days",
    "color",
    "sim_type",
    "condition_raw",
    "condition_level",
    "segment_id",
    "seller_id",
    "seller_is_platform",
    "price_tl",
    "usd_try",
    "us_launch_price_usd",
)
FLOATS = ("price_tl", "usd_try", "us_launch_price_usd")
INTEGERS = ("storage_gb", "generation", "device_age_days", "condition_level")
DATES = ("snapshot_date", "release_date")
TEXTS = ("offer_key", "device_id", "model", "series", "color", "sim_type", "condition_raw", "segment_id", "seller_id")

# An offer is an outlier (plan 9: absolute bounds and the segment median) when its dollar price is outside these
# multiples of the US launch price, or, in a segment with at least MIN_SEGMENT_OFFERS offers that day, outside these
# multiples of the day's segment median. On 12.10-7.11 no offer was below half of its segment median and 23 of 39,551
# were above twice it.
LAUNCH_BOUNDS = (0.1, 5.0)
MEDIAN_BOUNDS = (0.5, 2.0)
MIN_SEGMENT_OFFERS = 3
# A segment is thin when it had fewer offers than this in the 7 days up to a day (K30).
THIN_OFFERS = 5
DENSITY_DAYS = 7


class DatasetError(RuntimeError):
    """The offers could not be read, or a file lacks what the training set needs."""


def extract_sql(start: date | None, end: date | None) -> tuple[str, dict[str, object]]:
    conditions = ["platform = %(platform)s", "in_stock"]
    params: dict[str, object] = {"platform": PLATFORM}
    if start:
        conditions.append("snapshot_date >= %(start)s")
        params["start"] = start
    if end:
        conditions.append("snapshot_date <= %(end)s")
        params["end"] = end
    sql = (
        f"select {', '.join(COLUMNS)} from core.fct_offer_daily where {' and '.join(conditions)} "
        "order by snapshot_date, offer_key"
    )
    return sql, params


def extract(url: str, start: date | None = None, end: date | None = None) -> pd.DataFrame:
    """The daily offers of the period, read in a read-only transaction."""
    import psycopg

    sql, params = extract_sql(start, end)
    with psycopg.connect(url, connect_timeout=20) as conn:
        conn.read_only = True
        cursor = conn.execute(sql, params)
        rows = cursor.fetchall()
    return coerce(pd.DataFrame(rows, columns=list(COLUMNS)))


def coerce(daily: pd.DataFrame) -> pd.DataFrame:
    """The same column types whether the rows come from the database (Decimal, date) or from a file."""
    if missing := [column for column in COLUMNS if column not in daily.columns]:
        raise DatasetError(f"the offers lack {', '.join(missing)}")
    daily = daily.loc[:, list(COLUMNS)].copy()
    for column in FLOATS:
        daily[column] = pd.to_numeric(daily[column], errors="raise").astype("float64")
    for column in INTEGERS:
        daily[column] = pd.to_numeric(daily[column], errors="raise").astype("int64")
    for column in DATES:
        daily[column] = pd.to_datetime(daily[column]).astype("datetime64[ns]")
    for column in TEXTS:
        daily[column] = daily[column].astype("string")
    daily["seller_is_platform"] = daily["seller_is_platform"].astype(bool)
    return daily


def load(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise DatasetError(f"{path} not found: extract the offers first (python -m price_radar.model.dataset extract)")
    return coerce(pd.read_parquet(path))


def outliers(daily: pd.DataFrame) -> pd.Series:
    """True for an offer-day whose price is implausible against its launch price or its segment that day."""
    price_usd = daily["price_tl"] / daily["usd_try"]
    to_launch = price_usd / daily["us_launch_price_usd"]
    segment_day = price_usd.groupby([daily["snapshot_date"], daily["segment_id"]])
    to_median = price_usd / segment_day.transform("median")
    dense = segment_day.transform("size") >= MIN_SEGMENT_OFFERS
    off_launch = (to_launch < LAUNCH_BOUNDS[0]) | (to_launch > LAUNCH_BOUNDS[1])
    off_median = dense & ((to_median < MEDIAN_BOUNDS[0]) | (to_median > MEDIAN_BOUNDS[1]))
    return off_launch | off_median


def segment_offers(daily: pd.DataFrame, days: int = DENSITY_DAYS) -> pd.DataFrame:
    """Distinct offers of each segment in the `days` days up to each day: (segment_id, snapshot_date, offers)."""
    seen = daily[["segment_id", "snapshot_date", "offer_key"]].drop_duplicates()
    frames = []
    for day in pd.DatetimeIndex(seen["snapshot_date"].unique()).sort_values():
        window = seen[(seen["snapshot_date"] <= day) & (seen["snapshot_date"] > day - pd.Timedelta(days=days))]
        counts = window.groupby("segment_id")["offer_key"].nunique().rename("segment_offers").reset_index()
        counts["snapshot_date"] = day
        frames.append(counts)
    if not frames:
        return pd.DataFrame({"segment_id": [], "snapshot_date": [], "segment_offers": []})
    return pd.concat(frames, ignore_index=True)


def offer_price_pairs(daily: pd.DataFrame) -> pd.DataFrame:
    """One row per offer and price, with the features, rate and segment density of the day it was first seen."""
    daily = daily.sort_values(["snapshot_date", "offer_key"], kind="stable").reset_index(drop=True)
    daily["outlier"] = outliers(daily)
    keys = ["offer_key", "price_tl"]
    spans = daily.groupby(keys, sort=False)["snapshot_date"].agg(first_seen="min", last_seen="max", days_seen="size")
    first = daily.drop_duplicates(keys, keep="first")
    pairs = first.merge(spans.reset_index(), on=keys, validate="one_to_one")
    pairs = pairs.merge(
        segment_offers(daily).rename(columns={"snapshot_date": "first_seen"}),
        on=["segment_id", "first_seen"],
        how="left",
        validate="many_to_one",
    )
    pairs["price_usd"] = pairs["price_tl"] / pairs["usd_try"]
    pairs["log_price"] = np.log(pairs["price_usd"])
    pairs["age_years"] = pairs["device_age_days"] / 365.25
    pairs["thin"] = pairs["segment_offers"] < THIN_OFFERS
    return pairs.drop(columns=["snapshot_date", "device_age_days"]).reset_index(drop=True)


def summary(pairs: pd.DataFrame) -> list[str]:
    days = pairs["first_seen"].dt.date
    return [
        f"{len(pairs):,} offer-price pairs first seen {days.min()} - {days.max()}",
        f"{int(pairs['outlier'].sum()):,} outliers, {int(pairs['thin'].sum()):,} in thin segments",
    ]


def parse_day(text: str) -> date:
    return date.fromisoformat(text)


def main(argv: Iterable[str] | None = None) -> int:
    from price_radar.config import database_url

    parser = argparse.ArgumentParser(prog="python -m price_radar.model.dataset", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    pull = commands.add_parser("extract", help="read the daily offers into a local Parquet file")
    pull.add_argument("--out", type=Path, default=DEFAULT_OUT)
    pull.add_argument("--start", type=parse_day)
    pull.add_argument("--end", type=parse_day)
    args = parser.parse_args(list(argv) if argv is not None else None)

    url = database_url()
    if not url:
        print("NEON_DATABASE_URL is not set", file=sys.stderr)
        return 2
    try:
        daily = extract(url, args.start, args.end)
    except Exception as err:  # the message of a connection error may name the host
        print(f"extract failed: {type(err).__name__}", file=sys.stderr)
        return 1
    if daily.empty:
        print("no offers in the period", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    daily.to_parquet(args.out, index=False)
    print(f"wrote {len(daily):,} offer-days to {args.out}")
    for line in summary(offer_price_pairs(daily)):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
