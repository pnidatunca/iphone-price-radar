import io
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pyarrow.parquet as pq
import pytest

from price_radar.archive import parquet_export
from price_radar.archive.parquet_export import (
    OFFER_SNAPSHOTS,
    PIPELINE_RUNS,
    REJECTED_OFFERS,
    TABLES,
    ArchiveError,
    DagsHubStore,
    IsoWeek,
    _secret_parts,
    annotations,
    archive,
    describe,
    last_complete_week,
    markdown,
    message,
    parquet_file,
    rows_sha256,
)
from price_radar.config import ArchiveSettings
from price_radar.ingest.crawl import TURKEY
from price_radar.storage.snapshots import OFFER_COLUMNS, RUN_COLUMNS

NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)  # Tuesday of 2026-W45: W42-W44 are complete
FIRST_DAY = date(2026, 10, 12)  # Monday of 2026-W42, the first day of data
W42, W43, W44, W45 = (IsoWeek(2026, n) for n in (42, 43, 44, 45))
SETTINGS = ArchiveSettings(
    repository="owner/archive", s3_endpoint="https://dagshub.example/api/v1/repo-buckets/s3/owner", bucket="archive"
)


def offer(day: date, key: str = "getmobil:900101:7001", **changes):
    values = {
        "snapshot_date": day,
        "offer_key": key,
        "platform": "getmobil",
        "run_id": f"{day:%m%d}-1-getmobil",
        "fetched_at": datetime(day.year, day.month, day.day, 2, 5, tzinfo=UTC),
        "model": "iPhone 13",
        "storage_gb": 128,
        "color": "Mavi",
        "condition_raw": "Mükemmel",
        "sim_type": None,
        "battery_raw": None,
        "warranty_raw": None,
        "sku": None,
        "condition_flags": [],
        "seller_id": 7001,
        "is_buybox": True,
        "price_tl": Decimal("24999.00"),
        "list_price_tl": Decimal("24999.00"),
        "promo_pct": Decimal("0.00"),
        "stock_qty": 3,
        "in_stock": True,
        "product_url": "https://getmobil.com/satin-al/cep-telefonu/apple/iphone-13/apple-iphone-13-128-gb-mavi-900101/",
        "offer_url": "https://getmobil.com/satin-al/cep-telefonu/apple/iphone-13/apple-iphone-13-128-gb-mavi-900101/",
        "payload": '{"price": 24999}' if day == FIRST_DAY else None,  # stored only when the offer changed
        "payload_hash": "a" * 64,
        "parser_version": "getmobil-2",
    }
    return values | changes


def pipeline_run(day: date, **changes):
    started = datetime(day.year, day.month, day.day, 2, 0, tzinfo=UTC)
    values = {
        "run_id": f"{day:%m%d}-1-getmobil",
        "workflow": "daily-pipeline",
        "source": "getmobil",
        "snapshot_date": day,
        "started_at": started,
        "finished_at": started + timedelta(minutes=42),
        "exit_code": 0,
        "stop_reason": "completed",
        "requests": 700,
        "offers": 1,
        "in_stock_offers": 1,
        "rejected": 0,
        "warnings": 0,
        "failures": 0,
        "refusals": 0,
        "runtime_seconds": Decimal("2520.4"),
        "summary": '{"offers": 1}',
        "loaded_at": started + timedelta(minutes=43),
    }
    return values | changes


class FakeDatabase:
    """The rows of the three tables as dicts; ``record`` keeps the archive log."""

    def __init__(self, days: int = 23) -> None:  # 12.10 to 3.11: W42-W44 and two days of W45
        all_days = [FIRST_DAY + timedelta(days=n) for n in range(days)]
        self.tables = {
            OFFER_SNAPSHOTS.name: [offer(day) for day in all_days],
            REJECTED_OFFERS.name: [],
            PIPELINE_RUNS.name: [pipeline_run(day) for day in all_days],
        }
        self.records = []
        self.reads = []

    def platform_weeks(self, last):
        return {
            (r["source"], IsoWeek.of(r["snapshot_date"]))
            for r in self.tables[PIPELINE_RUNS.name]
            if r["snapshot_date"] <= last.sunday
        }

    def rows(self, table, platform, week):
        self.reads.append((table.name, week))
        return [
            tuple(row[field.name] for field in table.schema)
            for row in self.tables[table.name]
            if row[table.partition] == platform and week.monday <= row["snapshot_date"] <= week.sunday
        ]

    def latest(self):
        return {(f.table, f.platform, f.week): f for f in self.records}

    def record(self, file, run_id):
        self.records.append(file)


class FakeStore:
    def __init__(self, private: bool = True) -> None:
        self.private = private
        self.files: dict[str, bytes] = {}
        self.puts: list[str] = []

    def ensure_private(self):
        if not self.private:
            raise ArchiveError("DagsHub repository owner/archive is not private; nothing uploaded")

    def put(self, path, body):
        self.puts.append(path)
        self.files[path] = body

    def get(self, path):
        return self.files[path]

    def size(self, path):
        return len(self.files[path]) if path in self.files else None


def actions(outcomes):
    return {(o.file.table, str(o.file.week)): o.action for o in outcomes}


def read(body: bytes):
    return pq.read_table(io.BytesIO(body))


def test_iso_weeks_follow_the_calendar():
    assert IsoWeek.of(FIRST_DAY) == W42 and str(W42) == "2026-W42"
    assert (W42.monday, W42.sunday) == (date(2026, 10, 12), date(2026, 10, 18))
    assert IsoWeek.parse(" 2026-W44 ") == W44 and W44.shifted(1) == W45
    assert IsoWeek(2027, 1).shifted(-1) == IsoWeek(2026, 53)  # 2026 has 53 ISO weeks
    for text in ("2026-44", "2026-W4", "2025-W53"):
        with pytest.raises(ValueError):
            IsoWeek.parse(text)


def test_the_last_complete_week_is_counted_in_turkey_time():
    sunday_night = datetime(2026, 11, 1, 23, 59, tzinfo=TURKEY)
    assert last_complete_week(sunday_night) == W43
    assert last_complete_week(sunday_night + timedelta(minutes=1)) == W44  # Monday 00:00 in Turkey, 21:00 UTC
    assert last_complete_week(datetime(2026, 11, 2, 21, 0, tzinfo=UTC)) == W44  # a run GitHub started 8 hours late


def test_paths_name_the_table_platform_and_week():
    assert OFFER_SNAPSHOTS.path("getmobil", W44) == "raw/offer_snapshots/platform=getmobil/2026-W44.parquet"
    assert REJECTED_OFFERS.path("getmobil", W44) == "raw/rejected_offers/platform=getmobil/2026-W44.parquet"
    assert PIPELINE_RUNS.path("getmobil", W44) == "ops/pipeline_runs/source=getmobil/2026-W44.parquet"


def test_the_archive_schemas_follow_the_tables():
    assert tuple(OFFER_SNAPSHOTS.schema.names) == OFFER_COLUMNS
    assert tuple(PIPELINE_RUNS.schema.names) == (*RUN_COLUMNS, "loaded_at")
    assert REJECTED_OFFERS.schema.names == ["run_id", "snapshot_date", "platform", "offer_key", "reason"]
    assert "payload::text" in OFFER_SNAPSHOTS.select() and "summary::text" in PIPELINE_RUNS.select()
    assert "where platform = %s" in OFFER_SNAPSHOTS.select() and "where source = %s" in PIPELINE_RUNS.select()


def test_a_parquet_file_keeps_the_rows_as_they_are():
    rows = [tuple(row[f.name] for f in OFFER_SNAPSHOTS.schema) for row in (offer(FIRST_DAY), offer(W42.sunday))]
    body = parquet_file(OFFER_SNAPSHOTS, rows, {"iso_week": "2026-W42"})
    table = read(body)
    assert table.schema.remove_metadata() == OFFER_SNAPSHOTS.schema
    assert table.schema.metadata[b"iso_week"] == b"2026-W42"
    first, second = table.to_pylist()
    assert first == offer(FIRST_DAY)
    assert second["payload"] is None  # an unchanged day stays empty, it is not filled in
    assert second["price_tl"] == Decimal("24999.00") and second["fetched_at"].tzinfo is not None


def test_an_empty_week_still_gets_a_file_with_the_schema():
    table = read(parquet_file(REJECTED_OFFERS, [], {}))
    assert table.num_rows == 0 and table.schema.remove_metadata() == REJECTED_OFFERS.schema


def test_a_value_with_more_decimals_than_the_schema_is_an_error():
    rows = [tuple(offer(FIRST_DAY, price_tl=Decimal("24999.005"))[f.name] for f in OFFER_SNAPSHOTS.schema)]
    with pytest.raises(ArchiveError, match="price_tl value 24999.005 has more than 2 decimals"):
        parquet_file(OFFER_SNAPSHOTS, rows, {})


def test_the_rows_hash_ignores_the_time_zone_of_a_timestamp():
    utc = [tuple(offer(FIRST_DAY).values())]
    turkey = [tuple(offer(FIRST_DAY, fetched_at=offer(FIRST_DAY)["fetched_at"].astimezone(TURKEY)).values())]
    changed = [tuple(offer(FIRST_DAY, price_tl=Decimal("23999.00")).values())]
    assert rows_sha256(utc) == rows_sha256(turkey) != rows_sha256(changed)


def test_the_first_run_archives_every_complete_week():
    db, store = FakeDatabase(), FakeStore()
    last, outcomes = archive(db, store, now=NOW, recheck_weeks=2, run_id="1-1")
    assert last == W44
    assert actions(outcomes) == {(t.name, str(w)): "new" for w in (W42, W43, W44) for t in TABLES}
    assert len(store.files) == len(db.records) == 9  # nothing of the running week W45
    offers_w44 = store.files["raw/offer_snapshots/platform=getmobil/2026-W44.parquet"]
    assert read(offers_w44).num_rows == 7
    record = next(f for f in db.records if f.path.endswith("offer_snapshots/platform=getmobil/2026-W44.parquet"))
    assert (record.rows, record.first_day, record.last_day, record.size_bytes) == (
        7,
        date(2026, 10, 26),
        date(2026, 11, 1),
        len(offers_w44),
    )
    empty = next(f for f in db.records if f.table == REJECTED_OFFERS.name and f.week == W44)
    assert (empty.rows, empty.first_day) == (0, None)


def test_a_second_run_reads_only_the_recheck_window_and_uploads_nothing():
    db, store = FakeDatabase(), FakeStore()
    archive(db, store, now=NOW, recheck_weeks=2)
    db.reads.clear()
    _, outcomes = archive(db, store, now=NOW + timedelta(hours=8), recheck_weeks=2)
    assert len(store.puts) == 9
    assert {w for _, w in db.reads} == {W43, W44}
    assert set(actions(outcomes).items()) == {
        *(((t.name, "2026-W42"), "kept") for t in TABLES),
        *(((t.name, str(w)), "unchanged") for w in (W43, W44) for t in TABLES),
    }


def test_a_week_that_gained_rows_is_written_again():
    db, store = FakeDatabase(), FakeStore()
    archive(db, store, now=NOW, recheck_weeks=2)
    db.tables[OFFER_SNAPSHOTS.name].append(offer(W44.sunday, key="getmobil:900101:7002"))  # a late run
    _, outcomes = archive(db, store, now=NOW, recheck_weeks=2)
    changed = {key for key, action in actions(outcomes).items() if action == "changed"}
    assert changed == {(OFFER_SNAPSHOTS.name, "2026-W44")}
    assert read(store.files[OFFER_SNAPSHOTS.path("getmobil", W44)]).num_rows == 8
    assert db.latest()[(OFFER_SNAPSHOTS.name, "getmobil", W44)].rows == 8


def test_a_week_with_fewer_rows_than_its_archive_is_not_overwritten():
    db, store = FakeDatabase(), FakeStore()
    archive(db, store, now=NOW, recheck_weeks=2)
    before = dict(store.files)
    db.tables[OFFER_SNAPSHOTS.name] = [r for r in db.tables[OFFER_SNAPSHOTS.name] if r["snapshot_date"] != W44.monday]
    with pytest.raises(ArchiveError, match="6 rows now, 7 archived; the archive is not overwritten"):
        archive(db, store, now=NOW, recheck_weeks=2)
    assert store.files == before


def test_a_recorded_file_missing_from_the_store_is_uploaded_again():
    db, store = FakeDatabase(), FakeStore()
    archive(db, store, now=NOW, recheck_weeks=2)
    del store.files[PIPELINE_RUNS.path("getmobil", W44)]
    _, outcomes = archive(db, store, now=NOW, recheck_weeks=2)
    assert actions(outcomes)[(PIPELINE_RUNS.name, "2026-W44")] == "restored"
    assert PIPELINE_RUNS.path("getmobil", W44) in store.files


def test_a_chosen_week_is_read_again_whatever_its_age():
    db, store = FakeDatabase(), FakeStore()
    archive(db, store, now=NOW, recheck_weeks=2)
    db.reads.clear()
    _, outcomes = archive(db, store, now=NOW + timedelta(weeks=3), recheck_weeks=2, week=W42)
    assert {w for _, w in db.reads} == {W42}
    assert set(actions(outcomes).values()) == {"unchanged"}


def test_a_chosen_week_must_have_ended_and_have_runs():
    with pytest.raises(ArchiveError, match="2026-W45 has not ended yet in Turkey"):
        archive(FakeDatabase(), FakeStore(), now=NOW, recheck_weeks=2, week=W45)
    with pytest.raises(ArchiveError, match="no runs in 2026-W40"):
        archive(FakeDatabase(), FakeStore(), now=NOW, recheck_weeks=2, week=IsoWeek(2026, 40))


def test_nothing_is_uploaded_unless_the_repository_is_private():
    db, store = FakeDatabase(), FakeStore(private=False)
    with pytest.raises(ArchiveError, match="not private; nothing uploaded"):
        archive(db, store, now=NOW, recheck_weeks=2)
    assert store.puts == [] and db.records == []


def test_a_stored_file_that_differs_from_the_upload_is_not_recorded():
    class CorruptingStore(FakeStore):
        def get(self, path):
            return self.files[path][:-1]

    db = FakeDatabase()
    with pytest.raises(ArchiveError, match="differs from the upload"):
        archive(db, CorruptingStore(), now=NOW, recheck_weeks=2)
    assert db.records == []


def test_the_privacy_check_asks_the_dagshub_api():
    seen = []

    def repository(private):
        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"full_name": "owner/archive", "private": private})

        return httpx.MockTransport(handler)

    DagsHubStore(SETTINGS, "tok-secret", transport=repository(True)).ensure_private()
    assert str(seen[0].url) == "https://dagshub.com/api/v1/repos/owner/archive"
    assert seen[0].headers["Authorization"] == "token tok-secret"
    with pytest.raises(ArchiveError, match="not private"):
        DagsHubStore(SETTINGS, "tok-secret", transport=repository(False)).ensure_private()
    with pytest.raises(ArchiveError, match="HTTP 401"):
        DagsHubStore(
            SETTINGS, "tok-secret", transport=httpx.MockTransport(lambda r: httpx.Response(401))
        ).ensure_private()


def test_a_failure_is_described_without_secrets():
    url = "postgresql://neondb_owner:pw-0123456789@ep-example-123.eu-central-1.aws.neon.tech/neondb"
    error = RuntimeError("connection to ep-example-123.eu-central-1.aws.neon.tech failed\n(token tok-0123456789)")
    text = describe(error, _secret_parts("tok-0123456789", url))
    assert text == "RuntimeError: connection to *** failed (token ***)"
    assert describe(ArchiveError("raw/x.parquet: 6 rows now"), []) == "raw/x.parquet: 6 rows now"


def test_the_reports_hold_counts_weeks_and_paths():
    db, store = FakeDatabase(days=30), FakeStore()  # up to 10.11: W45 is complete a week later
    archive(db, store, now=NOW, recheck_weeks=2)
    last, outcomes = archive(db, store, now=NOW + timedelta(weeks=1), recheck_weeks=2)
    summary = markdown(last, outcomes, "archive")
    assert "Last complete week (Turkey time): 2026-W45. Files: 3 new, 3 unchanged, 6 kept." in summary
    assert "| raw.offer_snapshots | getmobil | 2026-W45 | 02.11–08.11 | 7 |" in summary
    assert "`raw/offer_snapshots/platform=getmobil/2026-W45.parquet`" in summary
    assert "2026-W42" not in summary  # kept files are counted, not listed
    text = message(last, outcomes)
    assert text.splitlines()[1:] == [
        "Last complete week 2026-W45: 3 new, 3 unchanged, 6 kept",
        "- getmobil 2026-W44: offer_snapshots 7 (unchanged), rejected_offers 0 (unchanged),"
        " pipeline_runs 7 (unchanged)",
        "- getmobil 2026-W45: offer_snapshots 7 (new), rejected_offers 0 (new), pipeline_runs 7 (new)",
    ]
    notices = annotations(last, outcomes)
    assert len(notices) == 3 and notices[2].startswith(
        "::notice title=Archive getmobil 2026-W45::raw.offer_snapshots 7 rows"
    )


def test_without_secrets_the_archive_does_not_start(monkeypatch, capsys):
    monkeypatch.setattr(parquet_export, "database_url", lambda: None)
    monkeypatch.setattr(parquet_export, "secret", lambda name: None)
    assert parquet_export.main([]) == 2
    assert "DAGSHUB_USER_TOKEN" in capsys.readouterr().out
