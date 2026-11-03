"""The device reference table ref.devices (plan 5.5), loaded from the seed file dbt/seeds/devices.csv.

    python -m price_radar.storage.devices load [--file dbt/seeds/devices.csv]
    python -m price_radar.storage.devices check [--summary FILE] [--annotate] [--notify always|problems|never]

One row per model and storage size, iPhone X and newer with SE 2020 and SE 2022 (decision K21): generation, series,
US release date, Apple's US launch price (USD, before sales tax) and, where a source states it, Apple Türkiye's launch
price (TL, with ÖTV and KDV). A Turkish price no source states stays empty; it is never estimated. Model names are
spelled as ``normalize.canonical_model`` writes them, so they join raw.offer_snapshots as they are.

Until dbt takes the file over as a seed (Phase 2), ``load`` writes the table: it replaces the contents with the file's
in one transaction, as ``dbt seed`` does. ``check`` lists what raw.offer_snapshots holds without a row: an unknown
model is an error (a new model, or a name the normaliser spells differently), an unknown storage size of a known model
a warning (a size the seed lacks, or a site's mistake).

Column rules:

- ``generation``: the number in the name. The X family (X, XS, XS Max, XR) is 10; a model without a number takes the
  flagship number current at its release (SE 2020: 11, SE 2022: 13, Air: 17, Duo: 18).
- ``series``: standart, mini, Plus, Pro, Pro Max, e, SE, Air or Duo (the foldable of 2026). The X family is placed by
  its successors: X and XS are Pro, XS Max is Pro Max, XR is standart (followed by the 11 Pro, 11 Pro Max and 11).
- ``release_date``: the first day on sale in the US.
- ``us_launch_price_usd``: the US price Apple announced. For the 12, 12 mini, 13, 13 mini, 14, 14 Plus, 15, 15 Plus
  and 17 that price assumed activation with a carrier; bought SIM-free, they cost $30 more.
- ``tr_launch_price_tl``: the price when Apple Türkiye first listed the model, weeks after the US launch up to the
  12 series (2020) and on the announcement day from the 13 series on. Later price rises are not used.
- ``source_url`` states the release date and US price, ``tr_source_url`` the Turkish price.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import psycopg
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from price_radar.config import REPO_ROOT, database_url
from price_radar.ingest.normalize import canonical_model
from price_radar.ingest.schemas import STORAGE_VALUES_GB
from price_radar.notify import telegram

log = logging.getLogger(__name__)

SEED = REPO_ROOT / "dbt" / "seeds" / "devices.csv"
COLUMNS = (
    "model",
    "generation",
    "series",
    "release_date",
    "storage_gb",
    "us_launch_price_usd",
    "tr_launch_price_tl",
    "source_url",
    "tr_source_url",
)
DDL = """
create schema if not exists ref;
create table if not exists ref.devices (
    model               text not null,
    generation          integer not null,
    series              text not null,
    release_date        date not null,
    storage_gb          integer not null,
    us_launch_price_usd numeric not null,
    tr_launch_price_tl  numeric,
    source_url          text not null,
    tr_source_url       text,
    primary key (model, storage_gb)
);
"""
SEEN_PAIRS = """
select model, storage_gb, count(*), max(snapshot_date)
from raw.offer_snapshots
group by model, storage_gb
order by model, storage_gb
"""


class Device(BaseModel):
    model: str
    generation: int = Field(ge=10)
    series: Literal["standart", "mini", "Plus", "Pro", "Pro Max", "e", "SE", "Air", "Duo"]
    release_date: date
    storage_gb: int
    us_launch_price_usd: int = Field(gt=0)
    tr_launch_price_tl: int | None = Field(default=None, gt=0)
    source_url: str
    tr_source_url: str | None = None

    @field_validator("tr_launch_price_tl", "tr_source_url", mode="before")
    @classmethod
    def _empty_is_none(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("model")
    @classmethod
    def _canonical(cls, value: str) -> str:
        if canonical_model(value) != value:
            raise ValueError(f"{value!r} is not spelled as normalize.canonical_model writes it")
        return value

    @field_validator("storage_gb")
    @classmethod
    def _storage(cls, value: int) -> int:
        if value not in STORAGE_VALUES_GB:
            raise ValueError(f"{value} GB is not an iPhone storage size")
        return value

    @field_validator("source_url", "tr_source_url")
    @classmethod
    def _https(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("https://"):
            raise ValueError("a source must be an https:// URL")
        return value

    @model_validator(mode="after")
    def _turkish_price_has_a_source(self) -> Device:
        if (self.tr_launch_price_tl is None) != (self.tr_source_url is None):
            raise ValueError("tr_launch_price_tl and tr_source_url go together")
        return self


def read_seed(path: Path = SEED) -> list[Device]:
    """The rows of the seed file, validated; one row per model and storage size."""
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if tuple(reader.fieldnames or ()) != COLUMNS:
            raise ValueError(f"{path.name}: the header must be {','.join(COLUMNS)}")
        devices, seen = [], set()
        for line, row in enumerate(reader, start=2):
            try:
                device = Device.model_validate(row)
            except ValidationError as err:
                raise ValueError(f"{path.name}:{line}: {err}") from err
            if (device.model, device.storage_gb) in seen:
                raise ValueError(f"{path.name}:{line}: {device.model} {device.storage_gb} GB appears twice")
            seen.add((device.model, device.storage_gb))
            devices.append(device)
    return devices


def load(conn: psycopg.Connection, devices: list[Device]) -> int:
    """Replace the table's contents with ``devices`` in one transaction. Returns the rows written."""
    placeholders = ", ".join(["%s"] * len(COLUMNS))
    with conn.transaction():
        conn.execute(DDL)
        conn.execute("delete from ref.devices")
        with conn.cursor() as cur:
            cur.executemany(
                f"insert into ref.devices ({', '.join(COLUMNS)}) values ({placeholders})",
                [tuple(getattr(device, column) for column in COLUMNS) for device in devices],
            )
    return len(devices)


@dataclass(frozen=True)
class Unmatched:
    model: str
    storage_gb: int
    offers: int  # rows in raw.offer_snapshots
    last_seen: date


@dataclass(frozen=True)
class Report:
    models_seen: int
    pairs_seen: int
    unknown_models: list[Unmatched]  # no row for the model at all
    unknown_sizes: list[Unmatched]  # the model has rows, but not for this storage size


def compare(seen: list[tuple[str, int, int, date]], known: set[tuple[str, int]]) -> Report:
    """Split the (model, storage, offers, last seen) groups of raw.offer_snapshots that ref.devices lacks."""
    known_models = {model for model, _ in known}
    unmatched = [Unmatched(*row) for row in seen if (row[0], row[1]) not in known]
    return Report(
        models_seen=len({row[0] for row in seen}),
        pairs_seen=len(seen),
        unknown_models=[u for u in unmatched if u.model not in known_models],
        unknown_sizes=[u for u in unmatched if u.model in known_models],
    )


def _line(kind: str, item: Unmatched) -> str:
    seen = f"{item.offers:,} offer rows, last seen {item.last_seen:%d.%m.%Y}"
    return f"{kind}: {item.model} {item.storage_gb} GB ({seen})"


def report_lines(report: Report) -> list[str]:
    lines = [
        f"{report.models_seen} models and {report.pairs_seen} model-storage pairs seen in raw.offer_snapshots;"
        f" {len(report.unknown_models)} unknown models, {len(report.unknown_sizes)} unknown sizes in ref.devices"
    ]
    lines += [_line("unknown model", item) for item in report.unknown_models]
    lines += [_line("unknown size", item) for item in report.unknown_sizes]
    return lines


def annotations(report: Report) -> list[str]:
    summary, *_ = report_lines(report)
    lines = [f"::notice title=ref.devices::{summary}"]
    lines += [f"::error title=Unknown model::{_line('unknown model', item)}" for item in report.unknown_models]
    lines += [f"::warning title=Unknown size::{_line('unknown size', item)}" for item in report.unknown_sizes]
    return lines


def check(conn: psycopg.Connection) -> Report:
    seen = conn.execute(SEEN_PAIRS).fetchall()
    known = set()
    if conn.execute("select to_regclass('ref.devices')").fetchone()[0] is not None:
        known = set(conn.execute("select model, storage_gb from ref.devices").fetchall())
    return compare(seen, known)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m price_radar.storage.devices")
    commands = parser.add_subparsers(dest="command", required=True)
    load_cmd = commands.add_parser("load", help="replace ref.devices with the seed file")
    load_cmd.add_argument("--file", type=Path, default=SEED)
    check_cmd = commands.add_parser("check", help="list models and sizes of raw.offer_snapshots without a row")
    check_cmd.add_argument("--summary", type=Path, help="append the report to this file (the job summary)")
    check_cmd.add_argument("--annotate", action="store_true", help="print GitHub Actions annotations")
    check_cmd.add_argument(
        "--notify",
        choices=["always", "problems", "never"],
        default="problems",
        help="send a Telegram message always, only for unknown models or sizes, or never",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")

    url = database_url()
    if not url:
        print("NEON_DATABASE_URL is not set (environment or .env)", file=sys.stderr)
        return 2
    with psycopg.connect(url, connect_timeout=30) as conn:
        if args.command == "load":
            devices = read_seed(args.file)
            count = load(conn, devices)
            log.info("ref.devices: %s rows for %s models", count, len({d.model for d in devices}))
            return 0
        report = check(conn)
    lines = report_lines(report)
    print("\n".join(lines))
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as f:
            f.write("### ref.devices\n\n" + "\n".join(f"- {line}" for line in lines) + "\n")
    if args.annotate:
        print("\n".join(annotations(report)))
    problems = bool(report.unknown_models or report.unknown_sizes)
    if args.notify == "always" or (args.notify == "problems" and problems):
        status = "ALERT" if report.unknown_models else "WARNING" if problems else "OK"
        sent = telegram.send("\n".join([f"[{status}] iPhone Price Radar, ref.devices check", *lines]))
        log.info("Telegram message %s", "sent" if sent else "not sent")
    return 1 if report.unknown_models else 0


if __name__ == "__main__":
    raise SystemExit(main())
