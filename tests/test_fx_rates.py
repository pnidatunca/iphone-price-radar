from datetime import date, timedelta
from decimal import Decimal

import pytest
from helpers import NOW

from price_radar.fx.tcmb import FxRate, archive_url
from price_radar.storage.fx_rates import COLUMNS, rate_rows, read_rates


def rate(day: date) -> FxRate:
    return FxRate(
        currency="USD",
        announced_on=day,
        valid_from=day + timedelta(days=1),
        bulletin_no="2026/999",
        unit=1,
        forex_buying=Decimal("40.1000"),
        forex_selling=Decimal("40.2000"),
        source_url=archive_url(day),
        fetched_at=NOW,
    )


def test_a_run_file_becomes_one_row_per_rate(tmp_path):
    path = tmp_path / "rates.jsonl"
    path.write_text(rate(date(2026, 10, 9)).model_dump_json() + "\n\n", encoding="utf-8")
    rows = rate_rows(read_rates(path))
    assert len(rows) == 1 and len(rows[0]) == len(COLUMNS)
    row = dict(zip(COLUMNS, rows[0], strict=True))
    assert (row["currency"], row["announced_on"], row["valid_from"]) == ("USD", date(2026, 10, 9), date(2026, 10, 10))
    assert (row["forex_selling"], row["banknote_selling"], row["fetched_at"]) == (Decimal("40.2000"), None, NOW)


def test_an_invalid_line_stops_the_load(tmp_path):
    path = tmp_path / "rates.jsonl"
    good = rate(date(2026, 10, 9)).model_dump_json()
    path.write_text(good + "\n" + good.replace('"valid_from":"2026-10-10"', '"valid_from":"2026-10-09"') + "\n")
    with pytest.raises(ValueError, match="rates.jsonl:2"):
        read_rates(path)
