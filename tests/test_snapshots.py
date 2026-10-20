import json

import pytest
from helpers import NOW, page

from price_radar.ingest.sources import getmobil
from price_radar.storage.snapshots import (
    OFFER_COLUMNS,
    RUN_COLUMNS,
    Run,
    offer_rows,
    read_run_dir,
    run_row,
    size_report,
)

VARIANT_URL = "https://getmobil.com/satin-al/cep-telefonu/apple/iphone-13/apple-iphone-13-128-gb-mavi-900101/"
SUMMARY = {
    "source": "getmobil",
    "snapshot_date": "2026-10-12",
    "started_at": "2026-10-12T00:17:00+00:00",
    "finished_at": "2026-10-12T00:59:00+00:00",
    "runtime_seconds": 2520.4,
    "stop_reason": "completed",
    "requests": {"made": 9, "budget": 1500},
    "offers": 6,
    "in_stock_offers": 6,
    "counters": {"offers_rejected": 1},
    "warnings": {"count": 2, "examples": []},
    "failures": {"count": 0, "examples": []},
    "refusals": {"count": 0, "examples": []},
}


def offers():
    result = getmobil.parse_variant_page(
        page("getmobil/variant_alternatives.html"), VARIANT_URL, fetched_at=NOW, snapshot_date=NOW.date()
    )
    return list(result.offers)


def write_run(run_dir, offer_list, rejections=(), summary=SUMMARY):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (run_dir / "offers.jsonl").write_text("".join(o.model_dump_json() + "\n" for o in offer_list), encoding="utf-8")
    (run_dir / "rejected.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rejections), encoding="utf-8")
    return run_dir


def test_a_run_directory_is_read_with_its_rejections(tmp_path):
    rejection = {"offer_key": "getmobil:900101:7099", "reason": "price_tl: Input should be greater than 0"}
    run = read_run_dir(write_run(tmp_path, offers(), [rejection]), "123-1-getmobil", "daily-pipeline", 0)
    assert (run.run_id, run.workflow, run.source, run.exit_code) == ("123-1-getmobil", "daily-pipeline", "getmobil", 0)
    assert len(run.offers) == 6 and run.rejections == [rejection]


def test_a_crashed_run_without_offer_files_still_loads_its_summary(tmp_path):
    tmp_path.joinpath("run_summary.json").write_text(json.dumps(SUMMARY), encoding="utf-8")
    run = read_run_dir(tmp_path, "123-1-getmobil", "daily-pipeline", 1)
    assert (run.offers, run.rejections) == ([], [])


def test_an_offer_from_another_source_is_refused(tmp_path):
    with pytest.raises(ValueError, match="a getmobil offer in a trendyol run"):
        read_run_dir(write_run(tmp_path, offers(), summary={**SUMMARY, "source": "trendyol"}), "r", "w", 0)


def test_the_run_row_carries_the_health_counts():
    row = dict(
        zip(RUN_COLUMNS, run_row(Run("123-1-getmobil", "daily-pipeline", "getmobil", SUMMARY, 0, [], [])), strict=True)
    )
    assert (row["snapshot_date"], row["exit_code"], row["stop_reason"]) == ("2026-10-12", 0, "completed")
    assert (row["requests"], row["offers"], row["rejected"], row["warnings"]) == (9, 6, 1, 2)
    assert row["summary"].obj == SUMMARY


def test_the_payload_is_stored_only_when_the_offer_changed():
    first, second, *rest = offers()
    run = Run("123-1-getmobil", "daily-pipeline", "getmobil", SUMMARY, 0, [first, second, *rest], [])
    # first: same hash as its last stored row; second: a different one; the rest: never seen before.
    rows = [
        dict(zip(OFFER_COLUMNS, row, strict=True))
        for row in offer_rows(run, {first.offer_key: first.payload_hash, second.offer_key: "0" * 64})
    ]
    assert rows[0]["payload"] is None
    assert rows[1]["payload"].obj == json.loads(second.model_dump_json())["payload"]
    assert all(row["payload"] is not None for row in rows[2:])
    assert all(row["payload_hash"] and row["run_id"] == "123-1-getmobil" for row in rows)
    assert (rows[0]["offer_key"], rows[0]["platform"], rows[0]["condition_flags"]) == (first.offer_key, "getmobil", [])


def test_size_report_warns_from_the_threshold_on():
    text, warn = size_report(120 * 1024 * 1024, warn_mb=350, limit_mb=500)
    assert text == "Neon database size: 120.0 MB of the 500 MB free limit (24%)"
    assert not warn
    assert size_report(350 * 1024 * 1024, warn_mb=350, limit_mb=500)[1]
