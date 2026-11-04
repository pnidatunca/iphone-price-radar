"""The dbt models on a real PostgreSQL with synthetic offers.

These tests wipe the raw, ref, ops and dbt schemas of the database RADAR_TEST_DATABASE_URL names, so they run only
when it is set and points to a local server: the service container of the CI job, or a scratch database. They load
six days in two batches, as two nightly runs would (with a fallback run adding an offer to a day already built),
check the figures, then rebuild everything at once and require the same marts.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlsplit

import psycopg
import pytest
from psycopg.types.json import Jsonb

from price_radar.analysis import run as analysis
from price_radar.storage import fx_rates, snapshots
from price_radar.transform import runner

TEST_URL = os.environ.get("RADAR_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_URL, reason="RADAR_TEST_DATABASE_URL is not set")

D1, D2, D3, D4, D5, D6 = (date(2026, 10, 17) + timedelta(days=n) for n in range(6))  # Saturday 17.10 to Thursday 22.10
# Bulletins announced on Friday 16.10 and Monday to Wednesday: Saturday to Monday use Friday's rate.
BULLETINS = {date(2026, 10, 16): "40", date(2026, 10, 19): "41", date(2026, 10, 20): "42", date(2026, 10, 21): "43"}
VARS = {"index_base_start": D3.isoformat(), "index_base_end": D4.isoformat(), "index_min_offers": 1}
READER = "radar_reader"
MARTS = {
    "daily_segment_prices": ("snapshot_date", "segment_id"),
    "daily_market": ("snapshot_date",),
    "daily_condition_premiums": ("snapshot_date", "condition_raw", "seller_group"),
    "daily_seller_positions": ("snapshot_date", "seller_key"),
    "offer_lifecycle": ("offer_key",),
    "price_index": ("snapshot_date",),
}
STAMPS = {"computed_at", "updated_at"}


def payload(seller: int, **seller_fields):
    return {"variant": {"title": "Yenilenmiş iPhone"}, "seller": {"vendor_id": seller, **seller_fields}}


def offer(day, key, price, *, model="iPhone 13", storage=128, condition="Mükemmel", data=None, listed=True):
    seller = int(key.rsplit(":", 1)[1])
    return {
        "snapshot_date": day,
        "offer_key": key,
        "platform": "getmobil",
        "run_id": f"run-{day:%m%d}",
        "fetched_at": datetime(day.year, day.month, day.day, 2, 0, tzinfo=UTC),
        "model": model,
        "storage_gb": storage,
        "color": "Mavi",
        "condition_raw": condition,
        "sim_type": "physical",
        "battery_raw": None,
        "warranty_raw": None,
        "sku": None,
        "condition_flags": [],
        "seller_id": seller,
        "is_buybox": seller == 1,
        "price_tl": Decimal(price),
        # rows of parser getmobil-1 had no list price; the payload kept the struck-through price
        "list_price_tl": Decimal(price) if listed else None,
        "promo_pct": Decimal("0.00") if listed else None,
        "stock_qty": 2,
        "in_stock": True,
        "product_url": "https://getmobil.com/satin-al/cep-telefonu/apple/iphone-13/x-100/",
        "offer_url": f"https://getmobil.com/satin-al/cep-telefonu/apple/iphone-13/x-100/?merchantId={seller}",
        "payload": Jsonb(data) if data is not None else None,
        "payload_hash": f"{key}:{price}".encode().hex()[:64].ljust(64, "0"),
        "parser_version": "getmobil-1" if not listed else "getmobil-2",
    }


A, B, C, D, F, G = (f"getmobil:{key}" for key in ("100:1", "100:2", "200:2", "300:3", "300:4", "300:1"))
FIRST_BATCH = [
    # A: Getmobil itself, one price, its payload stored once; no list price on the first two days
    offer(D1, A, "30000", data=payload(1, is_getmobil=True, score=9.1, strikethrough_price=33000), listed=False),
    offer(D2, A, "30000", listed=False),
    offer(D3, A, "30000"),
    offer(D4, A, "30000"),
    # B: another seller of the same variant cutting its price, missing on D4
    offer(D1, B, "31000", data=payload(2, is_getmobil=False, score=8.0)),
    offer(D2, B, "30500", data=payload(2, is_getmobil=False, score=8.0)),
    offer(D3, B, "30500"),
    offer(D1, C, "35000", storage=256, data=payload(2, is_getmobil=False)),
    *(offer(day, C, "35000", storage=256) for day in (D2, D3, D4)),
    offer(D1, D, "26000", condition="İyi", data=payload(3, is_getmobil=False)),
    *(offer(day, D, "26000", condition="İyi") for day in (D2, D3, D4)),
]
SECOND_BATCH = [
    # the fallback run of D4 found one more offer, loaded after D4 was built
    offer(D4, F, "25000", condition="İyi", data=payload(4, is_getmobil=False)) | {"run_id": "run-1020-fallback"},
    offer(D5, A, "30000"),
    offer(D6, A, "30000"),
    offer(D5, B, "29000", data=payload(2, is_getmobil=False, score=8.2)),
    offer(D6, B, "29000"),
    *(offer(day, C, "35000", storage=256) for day in (D5, D6)),
    *(offer(day, D, "26000", condition="İyi") for day in (D5, D6)),
    *(offer(day, F, "25000", condition="İyi") for day in (D5, D6)),
    # Getmobil itself also sells the İyi variant from D5: 27000 against its own 30000 Mükemmel offer
    offer(D5, G, "27000", condition="İyi", data=payload(1, is_getmobil=True)),
    offer(D6, G, "27000", condition="İyi"),
    # a model ref.devices lacks: a warning, not a failure
    offer(D6, "getmobil:900:5", "99999", model="iPhone 99", data=payload(5, is_getmobil=False)),
]


def run_row(run_id: str, day: date) -> dict:
    started = datetime(day.year, day.month, day.day, 2, 0, tzinfo=UTC)
    values = dict.fromkeys(snapshots.RUN_COLUMNS, 0)
    values |= {
        "run_id": run_id,
        "workflow": "test",
        "source": "getmobil",
        "snapshot_date": day,
        "started_at": started,
        "finished_at": started + timedelta(minutes=40),
        "stop_reason": "completed",
        "runtime_seconds": Decimal("2400"),
        "summary": Jsonb({}),
    }
    return values


def insert(conn: psycopg.Connection, table: str, rows: list[dict]) -> None:
    columns = list(rows[0])
    with conn.cursor() as cur:
        cur.executemany(
            f"insert into {table} ({', '.join(columns)}) values ({', '.join(['%s'] * len(columns))})",
            [tuple(row[c] for c in columns) for row in rows],
        )


def load(conn: psycopg.Connection, rows: list[dict]) -> None:
    runs = {(row["run_id"], row["snapshot_date"]) for row in rows}
    insert(conn, "ops.pipeline_runs", [run_row(run_id, day) for run_id, day in sorted(runs)])
    insert(conn, "raw.offer_snapshots", rows)


def build(**extra_vars):
    report = runner.build(runner.connection_variables(TEST_URL), extra=["--vars", json.dumps(VARS | extra_vars)])
    assert not report.failed, runner.report_lines(report)
    return report


def table(conn, name, key):
    rows = conn.execute(f"select * from marts.{name} order by {', '.join(key)}").fetchall()
    columns = [c.name for c in conn.execute(f"select * from marts.{name} limit 0").description]
    return [{c: v for c, v in zip(columns, row, strict=True) if c not in STAMPS} for row in rows]


@pytest.fixture(scope="module")
def db():
    assert urlsplit(TEST_URL).hostname in {"localhost", "127.0.0.1"}, "the dbt tests wipe their database"
    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        for schema in ("marts", "core", "staging", "raw", "ref", "ops"):
            conn.execute(f"drop schema if exists {schema} cascade")
        conn.execute(snapshots.DDL)
        conn.execute(fx_rates.DDL)
        insert(
            conn,
            "ref.fx_rates",
            [
                {
                    "currency": "USD",
                    "announced_on": day,
                    "valid_from": day + timedelta(days=1),
                    "unit": 1,
                    "forex_selling": Decimal(rate),
                    "source_url": f"https://www.tcmb.gov.tr/kurlar/{day:%Y%m}/{day:%d%m%Y}.xml",
                    "fetched_at": datetime(2026, 10, 22, tzinfo=UTC),
                    "parser_version": "tcmb-1",
                }
                for day, rate in BULLETINS.items()
            ],
        )
        if conn.execute("select 1 from pg_roles where rolname = %s", (READER,)).fetchone() is None:
            conn.execute(f"create role {READER} nologin")
        load(conn, FIRST_BATCH)
        first = build()
        load(conn, SECOND_BATCH)
        second = build()
        incremental = {name: table(conn, name, key) for name, key in MARTS.items()}
        yield conn, first, second, incremental


def test_the_first_build_is_clean_and_the_unknown_model_only_warns(db):
    _, first, second, _ = db
    assert not first.warned
    warned = [node.name for node in second.problems]
    assert warned and all(node.status == "warn" for node in second.problems)
    assert any(name.startswith("relationships_stg_offer_snapshots_device_id") for name in warned)


def test_staging_carries_the_payload_and_rebuilds_old_list_prices(db):
    conn, *_ = db
    rows = conn.execute(
        "select snapshot_date, payload_date, seller_is_platform, seller_score, list_price_tl, promo_pct,"
        " list_price_from_payload from staging.stg_offer_snapshots where offer_key = %s order by snapshot_date",
        (A,),
    ).fetchall()
    assert [row[1] for row in rows] == [D1] * 6  # the payload of D1 serves every later day
    assert {(row[2], row[3]) for row in rows} == {(True, Decimal("9.1"))}
    assert rows[0][4:] == (Decimal("33000"), Decimal("9.09"), True)  # struck-through price of the payload
    assert rows[2][4:] == (Decimal("30000.00"), Decimal("0.00"), False)
    scores = conn.execute(
        "select snapshot_date, seller_score from staging.stg_offer_snapshots where offer_key = %s order by 1", (B,)
    ).fetchall()
    assert [s for _, s in scores] == [Decimal("8.0")] * 3 + [Decimal("8.2")] * 2


def test_each_day_takes_the_rate_announced_before_it(db):
    conn, *_ = db
    rates = conn.execute(
        "select snapshot_date, min(usd_try), bool_and(fx_confirmed), min(price_usd) filter (where offer_key = %s)"
        " from core.fct_offer_daily group by 1 order by 1",
        (A,),
    ).fetchall()
    assert [(day, rate, confirmed) for day, rate, confirmed, _ in rates] == [
        (D1, 40, True),
        (D2, 40, True),
        (D3, 40, True),  # Monday: Friday's bulletin
        (D4, 41, True),
        (D5, 42, True),
        (D6, 43, False),  # no later bulletin yet
    ]
    assert rates[3][3] == Decimal("731.71")  # 30000 / 41


def test_the_lifecycle_merges_new_days_and_late_offers(db):
    conn, *_ = db
    rows = {
        row[0]: row[1:]
        for row in conn.execute(
            "select offer_key, first_seen, last_seen, days_seen, first_price_tl, last_price_tl, min_price_tl,"
            " max_price_tl, price_change_count, price_cut_count from marts.offer_lifecycle"
        ).fetchall()
    }
    # B: 31000 -> 30500 in the first build, -> 29000 after its missing day in the second
    assert rows[B] == (D1, D6, 5, 31000, 29000, 29000, 31000, 2, 2)
    assert rows[A] == (D1, D6, 6, 30000, 30000, 30000, 30000, 0, 0)
    assert rows[F][:3] == (D4, D6, 3)  # the fallback run's offer of a day built before


def test_segment_prices_include_what_a_later_run_added(db):
    conn, *_ = db
    row = conn.execute(
        "select offer_count, median_price_tl, seller_count from marts.daily_segment_prices"
        " where snapshot_date = %s and segment_id = 'iPhone 13 128 GB · İyi'",
        (D4,),
    ).fetchone()
    assert row == (2, Decimal("25500.00"), 2)
    market = conn.execute("select offer_count from marts.daily_market where snapshot_date = %s", (D4,)).fetchone()
    assert market == (4,)


def test_conditions_are_compared_within_a_seller(db):
    conn, *_ = db
    rows = conn.execute(
        "select snapshot_date, seller_group, pairs, median_difference_pct from marts.daily_condition_premiums"
        " where condition_raw = 'İyi' order by 1, 2"
    ).fetchall()
    # only Getmobil (seller 1) sells both conditions of the iPhone 13 128 GB, from D5 on; other sellers' İyi
    # offers have no Mükemmel offer of their own to compare with
    assert rows == [(day, group, 1, Decimal("-10.00")) for day in (D5, D6) for group in ("all", "platform")]


def test_sellers_are_placed_against_the_segment_median(db):
    conn, *_ = db
    rows = {
        (row[0], row[1]): row[2:]
        for row in conn.execute(
            "select snapshot_date, seller_key, segment_count, compared_segment_count, median_position_pct,"
            " below_median_count, above_median_count, cheapest_count from marts.daily_seller_positions"
        ).fetchall()
    }
    # From D5 the İyi segment has three sellers: 25000 (seller 4), 26000 (seller 3) and Getmobil's 27000. The
    # Mükemmel segments have one or two, too few to compare.
    for day in (D5, D6):
        assert rows[day, "getmobil:1"] == (2, 1, Decimal("3.85"), 0, 1, 0)
        assert rows[day, "getmobil:3"] == (1, 1, Decimal("0.00"), 0, 0, 0)
        assert rows[day, "getmobil:4"] == (1, 1, Decimal("-3.85"), 1, 0, 1)
        assert rows[day, "getmobil:2"] == (2, 0, None, 0, 0, 0)
    assert rows[D4, "getmobil:4"] == (1, 0, None, 0, 0, 0)  # two İyi sellers on D4


def test_the_analyses_run_on_the_marts(db):
    values = VARS | {"analysis_days": 2, "analysis_min_offers": 1, "analysis_min_compared": 1}
    compiled = analysis.compile_analyses(runner.connection_variables(TEST_URL), values)
    assert [a.name for a in compiled] == analysis.ORDER
    tables = {
        t.name: [dict(zip(t.columns, row, strict=True)) for row in t.rows]
        for t in analysis.run_analyses(TEST_URL, compiled)
    }

    (overview,) = tables["overview"]
    assert (overview["start_day"], overview["end_day"], overview["days"], overview["window_start"]) == (D1, D6, 6, D5)

    conditions = {row["condition"]: row for row in tables["a2_conditions"]}
    # D5 and D6: the İyi median 26000 against the Mükemmel median 29500 mixes sellers; Getmobil's own İyi offer is 10%
    # below its own Mükemmel offer
    assert conditions["İyi"]["segment_median_pct"] == Decimal("-11.9")
    assert (conditions["İyi"]["within_seller_pct"], conditions["İyi"]["within_seller_platform_pct"]) == (-10, -10)
    assert conditions["İyi"]["within_seller_others_pct"] is None

    platform, others = tables["a3_seller_positions"]
    assert (platform["sellers"], platform["mean_position_pct"], platform["cheapest_pct"]) == (
        "platform",
        Decimal("3.9"),
        0,
    )
    assert (others["active_sellers"], others["compared_segment_days"], others["mean_position_pct"]) == (
        4,
        4,
        Decimal("-1.9"),
    )
    assert (others["cheapest_pct"], others["mapped_sellers"], others["within_5_pct"]) == (50, 2, 2)
    assert len(tables["a3_seller_map"]) == 3

    # Flows 3 days away from the ends only: new offers from D4 (F, G and the unknown model, one a day), ended offers on
    # D2 to D4 (none: B was missing on D4 but returned)
    weeks = [
        (row["week_start"], row["days"], row["new_days"], row["new_offers_a_day"], row["ended_days"])
        for row in tables["a6_supply_weekly"]
    ]
    assert weeks == [(date(2026, 10, 12), 2, 0, None, 1), (date(2026, 10, 19), 4, 3, 1, 2)]
    assert {row["ended_offers_a_day"] for row in tables["a6_supply_weekly"]} == {0}
    (generation,) = tables["a1_age_curve"]
    assert (generation["generation"], generation["segments"]) == (13, 2)
    assert all(row["change_per_year_pct"] is None for row in tables["a1_fit"])  # one age: no slope
    step, median = tables["a1_storage_steps"]
    assert (step["from_gb"], step["to_gb"], step["apple_step_usd"]) == (128, 256, 100)
    assert median["model"] == "median of 1 steps"


def test_the_index_is_the_fixed_basket_formula_when_every_segment_is_priced(db):
    conn, *_ = db
    basket = conn.execute("select segment_id, weight, base_price_tl from marts.price_index_basket").fetchall()
    assert {segment for segment, *_ in basket} == {
        "iPhone 13 128 GB · Mükemmel",
        "iPhone 13 256 GB · Mükemmel",
        "iPhone 13 128 GB · İyi",
    }
    medians = dict(
        ((day, segment), price)
        for day, segment, price in conn.execute(
            "select snapshot_date, segment_id, median_price_tl from marts.daily_segment_prices"
        ).fetchall()
    )
    index = conn.execute("select snapshot_date, index_tl, segments_priced from marts.price_index order by 1").fetchall()
    assert [day for day, *_ in index] == [D3, D4, D5, D6]
    for day, value, priced in index:
        expected = 100 * sum(weight * medians[day, segment] / base for segment, weight, base in basket)
        assert priced == 3 and abs(value - expected) < Decimal("0.01"), (day, value, expected)


def test_the_reader_role_sees_the_marts_only(db):
    conn, *_ = db
    allowed = conn.execute(
        "select has_table_privilege(%s, 'marts.daily_segment_prices', 'select'),"
        " has_table_privilege(%s, 'raw.offer_snapshots', 'select')",
        (READER, READER),
    ).fetchone()
    assert allowed == (True, False)


def test_a_full_rebuild_gives_the_marts_the_incremental_runs_built(db):
    conn, _, _, incremental = db
    for schema in ("marts", "core"):
        conn.execute(f"drop schema {schema} cascade")
    build()
    for name, key in MARTS.items():
        assert table(conn, name, key) == incremental[name], name
