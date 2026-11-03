"""Weekly Parquet archive of the raw tables in the private DagsHub storage (plan 8.3, decision K6).

    python -m price_radar.archive.parquet_export [--week 2026-W44] [--run-id ID] [--summary FILE] [--annotate]
                                                 [--notify always|problems|never]

One file per table, platform and ISO week (Monday to Sunday by ``snapshot_date``) for raw.offer_snapshots,
raw.rejected_offers and ops.pipeline_runs, e.g. ``raw/offer_snapshots/platform=getmobil/2026-W44.parquet``. A file
holds the week's rows as the table has them: ``payload`` stays empty on the days an offer did not change, and the
jsonb columns are kept as their JSON text. A week without rows still gets a file (zero rows, same schema), so every
platform and week has all three.

Weeks are counted in Turkey time, so a run that GitHub starts hours late still archives the week that has ended.
Without ``--week``, every complete week that is not archived yet is archived (the first run takes everything since
12.10.2026, and a dropped Monday is caught up the next week), and the last ``recheck_weeks`` weeks are read again: a
week whose rows are unchanged (the same SHA-256 over its rows) is skipped, one that gained rows after it was archived
(a late run) is written again. A week that now has fewer rows than its archive is never overwritten; that is an
error. ``--week`` reads one week again, whatever its age.

Nothing is uploaded unless the DagsHub API says the repository is private. Each file is read back after the upload
(size, SHA-256, row count) and only then recorded in ops.archive_runs, which the retention step (plan 8.3) will
consult before it deletes detail rows. Messages and the job summary hold counts, weeks and paths only (K12, K19); the
token never appears in them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import boto3
import httpx
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from botocore.config import Config
from botocore.exceptions import ClientError

from price_radar.config import ArchiveSettings, database_url, load_settings, secret
from price_radar.ingest.crawl import TURKEY
from price_radar.notify import telegram

log = logging.getLogger(__name__)

DAGSHUB_API = "https://dagshub.com/api/v1"
TOKEN_NAME = "DAGSHUB_USER_TOKEN"
JSON_COLUMNS = {"payload", "summary"}  # jsonb: archived as its JSON text
MONEY = pa.decimal128(12, 2)  # prices are stored in kuruş precision (schemas.CENTS)
UTC_TIME = pa.timestamp("us", tz="UTC")

Action = Literal["new", "changed", "restored", "unchanged", "kept"]
ACTIONS: tuple[Action, ...] = ("new", "changed", "restored", "unchanged", "kept")


class ArchiveError(RuntimeError):
    """A failure whose message is safe to show: counts, weeks and paths only."""


@dataclass(frozen=True, order=True)
class IsoWeek:
    year: int
    week: int

    @classmethod
    def of(cls, day: date) -> IsoWeek:
        iso = day.isocalendar()
        return cls(iso.year, iso.week)

    @classmethod
    def parse(cls, text: str) -> IsoWeek:
        match = re.fullmatch(r"(\d{4})-W(\d{2})", text.strip())
        if match is None:
            raise ValueError(f"not an ISO week like 2026-W44: {text!r}")
        date.fromisocalendar(int(match[1]), int(match[2]), 1)  # week 53 of a 52-week year raises ValueError
        return cls(int(match[1]), int(match[2]))

    @property
    def monday(self) -> date:
        return date.fromisocalendar(self.year, self.week, 1)

    @property
    def sunday(self) -> date:
        return self.monday + timedelta(days=6)

    def shifted(self, weeks: int) -> IsoWeek:
        return IsoWeek.of(self.monday + timedelta(weeks=weeks))

    def __str__(self) -> str:
        return f"{self.year}-W{self.week:02d}"


def last_complete_week(now: datetime) -> IsoWeek:
    """The latest ISO week that has ended in Turkey: from Monday 00:00 Turkey time on, the week before."""
    return IsoWeek.of(now.astimezone(TURKEY).date()).shifted(-1)


@dataclass(frozen=True)
class ArchiveTable:
    name: str  # schema-qualified
    partition: str  # the column that names the platform
    order_by: str  # a full order, so the same rows always give the same file and hash
    schema: pa.Schema

    def path(self, platform: str, week: IsoWeek) -> str:
        return f"{self.name.replace('.', '/')}/{self.partition}={platform}/{week}.parquet"

    def select(self) -> str:
        columns = ", ".join(f"{f.name}::text" if f.name in JSON_COLUMNS else f.name for f in self.schema)
        return (
            f"select {columns} from {self.name}"
            f" where {self.partition} = %s and snapshot_date between %s and %s order by {self.order_by}"
        )


OFFER_SNAPSHOTS = ArchiveTable(
    "raw.offer_snapshots",
    partition="platform",
    order_by="snapshot_date, offer_key",
    schema=pa.schema(
        [
            ("snapshot_date", pa.date32()),
            ("offer_key", pa.string()),
            ("platform", pa.string()),
            ("run_id", pa.string()),
            ("fetched_at", UTC_TIME),
            ("model", pa.string()),
            ("storage_gb", pa.int32()),
            ("color", pa.string()),
            ("condition_raw", pa.string()),
            ("sim_type", pa.string()),
            ("battery_raw", pa.string()),
            ("warranty_raw", pa.string()),
            ("sku", pa.string()),
            ("condition_flags", pa.list_(pa.string())),
            ("seller_id", pa.int64()),
            ("is_buybox", pa.bool_()),
            ("price_tl", MONEY),
            ("list_price_tl", MONEY),
            ("promo_pct", pa.decimal128(5, 2)),
            ("stock_qty", pa.int32()),
            ("in_stock", pa.bool_()),
            ("product_url", pa.string()),
            ("offer_url", pa.string()),
            ("payload", pa.string()),
            ("payload_hash", pa.string()),
            ("parser_version", pa.string()),
        ]
    ),
)
REJECTED_OFFERS = ArchiveTable(
    "raw.rejected_offers",
    partition="platform",
    order_by="snapshot_date, offer_key, run_id, reason",
    schema=pa.schema(
        [
            ("run_id", pa.string()),
            ("snapshot_date", pa.date32()),
            ("platform", pa.string()),
            ("offer_key", pa.string()),
            ("reason", pa.string()),
        ]
    ),
)
PIPELINE_RUNS = ArchiveTable(
    "ops.pipeline_runs",
    partition="source",
    order_by="started_at, run_id",
    schema=pa.schema(
        [
            ("run_id", pa.string()),
            ("workflow", pa.string()),
            ("source", pa.string()),
            ("snapshot_date", pa.date32()),
            ("started_at", UTC_TIME),
            ("finished_at", UTC_TIME),
            ("exit_code", pa.int32()),
            ("stop_reason", pa.string()),
            ("requests", pa.int32()),
            ("offers", pa.int32()),
            ("in_stock_offers", pa.int32()),
            ("rejected", pa.int32()),
            ("warnings", pa.int32()),
            ("failures", pa.int32()),
            ("refusals", pa.int32()),
            ("runtime_seconds", pa.decimal128(12, 3)),
            ("summary", pa.string()),
            ("loaded_at", UTC_TIME),
        ]
    ),
)
TABLES = (OFFER_SNAPSHOTS, REJECTED_OFFERS, PIPELINE_RUNS)

ARCHIVE_DDL = """
create schema if not exists ops;
create table if not exists ops.archive_runs (
    table_name        text not null,
    platform          text not null,
    iso_week          text not null,
    week_start        date not null,
    row_count         integer not null,
    min_snapshot_date date,
    max_snapshot_date date,
    content_sha256    text not null,
    file_sha256       text not null,
    size_bytes        bigint not null,
    path              text not null,
    run_id            text,
    archived_at       timestamptz not null default now(),
    primary key (table_name, platform, iso_week, archived_at)
);
"""
LATEST_FILES = """
select distinct on (table_name, platform, iso_week)
       table_name, platform, iso_week, row_count, min_snapshot_date, max_snapshot_date, content_sha256, file_sha256,
       size_bytes, path
from ops.archive_runs
order by table_name, platform, iso_week, archived_at desc
"""
PLATFORM_DAYS = "select distinct source, snapshot_date from ops.pipeline_runs where snapshot_date <= %s"


@dataclass(frozen=True)
class ArchivedFile:
    table: str
    platform: str
    week: IsoWeek
    rows: int
    first_day: date | None
    last_day: date | None
    content_sha256: str  # over the rows (rows_sha256): decides whether a week changed
    file_sha256: str  # over the Parquet file: checks the stored copy
    size_bytes: int
    path: str


@dataclass(frozen=True)
class Outcome:
    file: ArchivedFile
    action: Action


class Store(Protocol):
    def ensure_private(self) -> None: ...
    def put(self, path: str, body: bytes) -> None: ...
    def get(self, path: str) -> bytes: ...
    def size(self, path: str) -> int | None: ...


class DagsHubStore:
    """The S3-compatible bucket of the private DagsHub repository
    (https://dagshub.com/docs/feature_guide/dagshub_storage/). The token is both the access key and the secret key."""

    def __init__(self, settings: ArchiveSettings, token: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self._token = token
        self._transport = transport
        self._s3 = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            region_name=settings.region,
            aws_access_key_id=token,
            aws_secret_access_key=token,
            config=Config(
                s3={"addressing_style": "path"},
                retries={"max_attempts": 5, "mode": "standard"},
                # S3-compatible stores may refuse the checksum headers that newer clients add by default.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    def ensure_private(self) -> None:
        with httpx.Client(timeout=30, transport=self._transport) as client:
            response = client.get(
                f"{DAGSHUB_API}/repos/{self.settings.repository}", headers={"Authorization": f"token {self._token}"}
            )
        if response.status_code != 200:
            raise ArchiveError(f"DagsHub repository check failed (HTTP {response.status_code}); nothing uploaded")
        if response.json().get("private") is not True:
            raise ArchiveError(f"DagsHub repository {self.settings.repository} is not private; nothing uploaded")

    def put(self, path: str, body: bytes) -> None:
        self._s3.put_object(
            Bucket=self.settings.bucket, Key=path, Body=body, ContentType="application/vnd.apache.parquet"
        )

    def get(self, path: str) -> bytes:
        return self._s3.get_object(Bucket=self.settings.bucket, Key=path)["Body"].read()

    def size(self, path: str) -> int | None:
        try:
            return self._s3.head_object(Bucket=self.settings.bucket, Key=path)["ContentLength"]
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise


class Database:
    """The archive's reads and writes in Neon. The connection runs in autocommit mode, so each record is committed
    as soon as its file is verified."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def platform_weeks(self, last: IsoWeek) -> set[tuple[str, IsoWeek]]:
        """The (platform, week) pairs with at least one run, up to ``last``."""
        if self.conn.execute("select to_regclass('ops.pipeline_runs')").fetchone()[0] is None:
            return set()
        rows = self.conn.execute(PLATFORM_DAYS, (last.sunday,)).fetchall()
        return {(source, IsoWeek.of(day)) for source, day in rows}

    def rows(self, table: ArchiveTable, platform: str, week: IsoWeek) -> list[tuple[Any, ...]]:
        return self.conn.execute(table.select(), (platform, week.monday, week.sunday)).fetchall()

    def latest(self) -> dict[tuple[str, str, IsoWeek], ArchivedFile]:
        """The latest record of every archived file."""
        if self.conn.execute("select to_regclass('ops.archive_runs')").fetchone()[0] is None:
            return {}
        files = [
            ArchivedFile(table, platform, IsoWeek.parse(week), *rest)
            for table, platform, week, *rest in self.conn.execute(LATEST_FILES).fetchall()
        ]
        return {(f.table, f.platform, f.week): f for f in files}

    def record(self, file: ArchivedFile, run_id: str | None) -> None:
        with self.conn.transaction():
            self.conn.execute(ARCHIVE_DDL)
            self.conn.execute(
                "insert into ops.archive_runs (table_name, platform, iso_week, week_start, row_count,"
                " min_snapshot_date, max_snapshot_date, content_sha256, file_sha256, size_bytes, path, run_id)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    file.table,
                    file.platform,
                    str(file.week),
                    file.week.monday,
                    file.rows,
                    file.first_day,
                    file.last_day,
                    file.content_sha256,
                    file.file_sha256,
                    file.size_bytes,
                    file.path,
                    run_id,
                ),
            )


def rows_sha256(rows: Iterable[Sequence[Any]]) -> str:
    """SHA-256 over the rows in a fixed text form. It does not depend on the Parquet writer, so a pyarrow upgrade
    does not make an unchanged week look changed."""
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps([_plain(value) for value in row], ensure_ascii=False, separators=(",", ":")).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date | Decimal):
        return str(value)
    return value


def parquet_file(table: ArchiveTable, rows: Sequence[Sequence[Any]], metadata: dict[str, str]) -> bytes:
    columns = list(zip(*rows, strict=True)) if rows else [()] * len(table.schema)
    arrays = [_array(field, list(values)) for field, values in zip(table.schema, columns, strict=True)]
    data = pa.Table.from_arrays(arrays, schema=table.schema.with_metadata(metadata))
    sink = pa.BufferOutputStream()
    pq.write_table(data, sink)
    return sink.getvalue().to_pybytes()


def _array(field: pa.Field, values: list[Any]) -> pa.Array:
    if pa.types.is_decimal(field.type):
        for value in values:
            exponent = None if value is None else value.as_tuple().exponent
            if isinstance(exponent, int) and -exponent > field.type.scale:
                raise ArchiveError(f"{field.name} value {value} has more than {field.type.scale} decimals")
    elif pa.types.is_timestamp(field.type):
        values = [None if value is None else value.astimezone(UTC) for value in values]
    return pa.array(values, type=field.type)


def verify(store: Store, path: str, body: bytes, rows: int) -> None:
    """Read the stored file back: the same bytes, and a Parquet file with the rows written."""
    stored = store.get(path)
    if hashlib.sha256(stored).digest() != hashlib.sha256(body).digest():
        raise ArchiveError(f"{path}: the stored file ({len(stored)} bytes) differs from the upload ({len(body)} bytes)")
    stored_rows = pq.read_table(pa.BufferReader(stored)).num_rows
    if stored_rows != rows:
        raise ArchiveError(f"{path}: {stored_rows} rows read back, {rows} written")


def archive_file(
    db: Database,
    store: Store,
    table: ArchiveTable,
    platform: str,
    week: IsoWeek,
    known: ArchivedFile | None,
    run_id: str | None,
) -> Outcome:
    """Archive one table's week for one platform unless the archive already holds exactly these rows."""
    rows = db.rows(table, platform, week)
    content = rows_sha256(rows)
    path = table.path(platform, week)
    action: Action
    if known is None:
        action = "new"
    elif known.content_sha256 == content:
        if store.size(path) == known.size_bytes:
            return Outcome(known, "unchanged")
        action = "restored"  # recorded, but the stored file is missing or not the one recorded
    elif len(rows) < known.rows:
        raise ArchiveError(
            f"{path}: {len(rows):,} rows now, {known.rows:,} archived; the archive is not overwritten with fewer rows"
        )
    else:
        action = "changed"
    day_index = table.schema.get_field_index("snapshot_date")
    days = [row[day_index] for row in rows]
    metadata = {"table": table.name, "platform": platform, "iso_week": str(week), "content_sha256": content}
    body = parquet_file(table, rows, metadata)
    store.put(path, body)
    verify(store, path, body, len(rows))
    file = ArchivedFile(
        table.name,
        platform,
        week,
        len(rows),
        min(days, default=None),
        max(days, default=None),
        content,
        hashlib.sha256(body).hexdigest(),
        len(body),
        path,
    )
    db.record(file, run_id)
    return Outcome(file, action)


def archive(
    db: Database,
    store: Store,
    *,
    now: datetime,
    recheck_weeks: int,
    week: IsoWeek | None = None,
    run_id: str | None = None,
) -> tuple[IsoWeek, list[Outcome]]:
    """Archive the complete weeks (see the module docstring). Returns the last complete week and one outcome per file;
    files of weeks before the recheck window that are already archived are ``kept`` without being read."""
    last = last_complete_week(now)
    if week is not None and week > last:
        raise ArchiveError(f"{week} has not ended yet in Turkey (last complete week: {last})")
    targets = sorted(
        (pair for pair in db.platform_weeks(last) if week is None or pair[1] == week), key=lambda p: (p[1], p[0])
    )
    if week is not None and not targets:
        raise ArchiveError(f"no runs in {week}")
    store.ensure_private()
    latest = db.latest()
    recheck_from = last.shifted(1 - recheck_weeks)
    outcomes = []
    for platform, target in targets:
        for table in TABLES:
            known = latest.get((table.name, platform, target))
            if known is not None and week is None and target < recheck_from:
                outcomes.append(Outcome(known, "kept"))
                continue
            outcomes.append(archive_file(db, store, table, platform, target, known, run_id))
    return last, outcomes


def counts_text(outcomes: list[Outcome]) -> str:
    counts = Counter(outcome.action for outcome in outcomes)
    return ", ".join(f"{counts[action]} {action}" for action in ACTIONS if counts[action]) or "nothing to archive"


def _days(file: ArchivedFile) -> str:
    return f"{file.first_day:%d.%m}–{file.last_day:%d.%m}" if file.first_day and file.last_day else "–"


def markdown(last: IsoWeek, outcomes: list[Outcome], bucket: str) -> str:
    lines = [
        "### Weekly archive",
        "",
        f"Last complete week (Turkey time): {last}. Files: {counts_text(outcomes)}. Bucket: `{bucket}`.",
    ]
    shown = [outcome for outcome in outcomes if outcome.action != "kept"]
    if shown:
        lines += ["", "| Table | Platform | Week | Days | Rows | Size | Action | Path |"]
        lines += ["|---|---|---|---|---:|---:|---|---|"]
        for outcome in shown:
            f = outcome.file
            lines.append(
                f"| {f.table} | {f.platform} | {f.week} | {_days(f)} | {f.rows:,} | {f.size_bytes / 1024:,.1f} KB"
                f" | {outcome.action} | `{f.path}` |"
            )
    return "\n".join(lines) + "\n"


def _by_week(outcomes: list[Outcome]) -> dict[tuple[IsoWeek, str], list[Outcome]]:
    weeks: dict[tuple[IsoWeek, str], list[Outcome]] = {}
    for outcome in outcomes:
        if outcome.action != "kept":
            weeks.setdefault((outcome.file.week, outcome.file.platform), []).append(outcome)
    return weeks


def message(last: IsoWeek, outcomes: list[Outcome]) -> str:
    """The Telegram text: one line per platform and week that was read."""
    lines = ["[OK] iPhone Price Radar, weekly archive", f"Last complete week {last}: {counts_text(outcomes)}"]
    for (week, platform), group in _by_week(outcomes).items():
        files = ", ".join(f"{o.file.table.split('.')[1]} {o.file.rows:,} ({o.action})" for o in group)
        lines.append(f"- {platform} {week}: {files}")
    return "\n".join(lines)


def annotations(last: IsoWeek, outcomes: list[Outcome]) -> list[str]:
    """One notice per platform and week (GitHub shows at most 10 notices per step)."""
    lines = [f"::notice title=Weekly archive::Last complete week {last}: {counts_text(outcomes)}"]
    for (week, platform), group in _by_week(outcomes).items():
        files = ", ".join(f"{o.file.table} {o.file.rows} rows {o.file.size_bytes} bytes ({o.action})" for o in group)
        lines.append(f"::notice title=Archive {platform} {week}::{files}")
    return lines


def describe(error: BaseException, secrets: Iterable[str | None]) -> str:
    """A failure as one line that is safe to log and send: an ``ArchiveError`` as it is, anything else with its type,
    and any secret (or part of the database URL) replaced by ***."""
    text = str(error) if isinstance(error, ArchiveError) else f"{type(error).__name__}: {error}"
    for value in secrets:
        if value and len(value) >= 4:
            text = text.replace(value, "***")
    return " ".join(text.split())


def _secret_parts(token: str | None, url: str | None) -> list[str | None]:
    parts = [token, url]
    if url:
        split = urlsplit(url)
        parts += [split.hostname, split.username, split.password]
    return parts


def _quiet_clients() -> None:
    # Debug logs of the S3 client carry the signed headers, and with them the access key, which is the token.
    for name in ("httpx", "boto3", "botocore", "s3transfer", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m price_radar.archive.parquet_export")
    parser.add_argument(
        "--week",
        type=IsoWeek.parse,
        help="one ISO week to read again, e.g. 2026-W44 (default: the weeks not archived yet and the recheck window)",
    )
    parser.add_argument("--run-id", help="recorded with each file in ops.archive_runs")
    parser.add_argument("--summary", type=Path, help="append a Markdown summary to this file (the job summary)")
    parser.add_argument("--annotate", action="store_true", help="print GitHub Actions annotations")
    parser.add_argument(
        "--notify",
        choices=["always", "problems", "never"],
        default="problems",
        help="send a Telegram message always, only on failure, or never",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    _quiet_clients()
    settings = load_settings().archive
    url, token = database_url(), secret(TOKEN_NAME)
    if settings is None or not url or not token:
        print(f"the archive needs settings.archive, NEON_DATABASE_URL and {TOKEN_NAME} (environment or .env)")
        return 2
    try:
        store = DagsHubStore(settings, token)
        with psycopg.connect(url, connect_timeout=30, autocommit=True) as conn:
            last, outcomes = archive(
                Database(conn),
                store,
                now=datetime.now(UTC),
                recheck_weeks=settings.recheck_weeks,
                week=args.week,
                run_id=args.run_id,
            )
    except Exception as error:  # every failure is reported the same way, without secrets
        text = describe(error, _secret_parts(token, url))
        print(f"weekly archive failed: {text}")
        if args.annotate:
            print(f"::error title=Weekly archive::{text}")
        if args.notify != "never":
            telegram.send(f"[ALERT] iPhone Price Radar, weekly archive\n{text}")
        return 1
    summary = markdown(last, outcomes, settings.bucket)
    print(summary)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as f:
            f.write(summary)
    if args.annotate:
        print("\n".join(annotations(last, outcomes)))
    if args.notify == "always":
        sent = telegram.send(message(last, outcomes))
        log.info("Telegram message %s", "sent" if sent else "not sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
