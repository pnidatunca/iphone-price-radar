import json

import pytest

from price_radar.config import database_url
from price_radar.storage.landing import offer_rows, read_run

SUMMARY = {
    "source": "easycep",
    "started_at": "2026-10-12T00:17:00+00:00",
    "finished_at": "2026-10-12T00:20:00+00:00",
    "offers": 2,
}


def offer(key: str, platform: str = "easycep") -> dict:
    return {
        "platform": platform,
        "offer_key": key,
        "snapshot_date": "2026-10-12",
        "fetched_at": "2026-10-12T00:18:00+00:00",
        "price_tl": "30000.00",
    }


def write_run(run_dir, offers=None, summary=SUMMARY):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    if offers is not None:
        lines = [json.dumps(o) for o in offers]
        (run_dir / "offers.jsonl").write_text("\n".join(lines) + "\n\n", encoding="utf-8")
    return run_dir


def test_run_directory_becomes_one_row_per_offer(tmp_path):
    run = read_run(write_run(tmp_path, [offer("easycep:1"), offer("easycep:2")]), "123-easycep")
    rows = offer_rows(run)
    assert [row[:5] for row in rows] == [
        ("123-easycep", "easycep:1", "easycep", "2026-10-12", "2026-10-12T00:18:00+00:00"),
        ("123-easycep", "easycep:2", "easycep", "2026-10-12", "2026-10-12T00:18:00+00:00"),
    ]
    assert rows[0][5].obj == offer("easycep:1")  # the whole offer document is stored unchanged


def test_run_without_offers_file_still_loads_its_summary(tmp_path):
    run = read_run(write_run(tmp_path), "123-easycep")
    assert run.offers == []
    assert run.summary == SUMMARY


def test_offer_from_another_source_is_refused(tmp_path):
    run_dir = write_run(tmp_path, [offer("getmobil:1:2", platform="getmobil")])
    with pytest.raises(ValueError):
        read_run(run_dir, "123-easycep")


def test_database_url_prefers_the_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("NEON_DATABASE_URL=postgresql://from-file\n", encoding="utf-8")
    monkeypatch.setenv("NEON_DATABASE_URL", "postgresql://from-env")
    assert database_url(env_file) == "postgresql://from-env"


def test_database_url_falls_back_to_the_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text('# local\nOTHER=1\nNEON_DATABASE_URL="postgresql://from-file"\n', encoding="utf-8")
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    assert database_url(env_file) == "postgresql://from-file"
    assert database_url(tmp_path / "missing.env") is None
