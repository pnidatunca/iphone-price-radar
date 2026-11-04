"""The report charts, drawn from analysis tables written the way python -m price_radar.analysis.run writes them."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal as D

import pytest

from price_radar.analysis import charts
from price_radar.analysis import run as analysis

SVG = "{http://www.w3.org/2000/svg}svg"
SELLER_MAP = ["seller_rank", "is_platform", "offers_a_day", "mean_position_pct", "position_band"]


def tables(**changes: tuple[list[str], list[tuple]]) -> list[analysis.Table]:
    base = {
        "overview": (
            ["start_day", "end_day", "days", "window_start", "window_days"],
            [(date(2026, 10, 12), date(2026, 11, 4), 24, date(2026, 10, 29), 7)],
        ),
        "a1_age_curve": (
            ["generation", "segments", "age_years", "price_to_us_launch", "vs_next_newer_generation_pct"],
            [
                (17, 10, D("1.1"), D("2.322"), None),
                (16, 12, D("2.1"), D("1.824"), D("-21.5")),
                (12, 11, D("6.0"), D("0.638"), D("-32.0")),
            ],
        ),
        "a1_fit": (
            ["models", "segments", "youngest_years", "oldest_years", "change_per_year_pct", "r2", "ratio_at_1_year"],
            [("all models", 90, D("1.1"), D("8.1"), D("-22.1"), D("0.952"), D("2.33"))],
        ),
        "a2_conditions": (
            ["condition", "offers", "segment_median_pct", "within_seller_pairs", "within_seller_pct"],
            [
                ("Mükemmel", 7097, None, None, None),
                ("Premium+", 2407, D("-3.5"), 382, D("3.8")),
                ("Çok İyi", 1, D("-16.3"), 1, D("-4.3")),
                ("İyi", 1170, D("-6.3"), 382, D("-6.1")),
            ],
        ),
        "a3_seller_map": (
            SELLER_MAP,
            [
                (1, True, D("289.4"), D("-13.0"), "below"),
                (2, False, D("44.9"), D("8.7"), "above"),
                (3, False, D("36.0"), D("-2.5"), "within"),
                (4, False, D("34.6"), D("-5.0"), "below"),  # -5.04 before rounding: the band comes from the analysis
            ],
        ),
        "a6_supply_by_generation": (
            ["generation", "first_window_offers_a_day", "last_window_offers_a_day", "change_pct"],
            [
                (17, D("82.7"), D("105.4"), D("27.5")),
                (12, D("179.4"), D("144.3"), D("-19.6")),
                (11, None, D("3.0"), None),
            ],
        ),
    }
    base.update(changes)
    return [analysis.Table(name, "", columns, rows) for name, (columns, rows) in base.items()]


@pytest.fixture
def data(tmp_path):
    out = tmp_path / "analyses"
    analysis.write(tables(), out, "page")
    return out


def names(paths):
    return sorted(path.name for path in paths)


def test_every_chart_is_drawn_for_both_themes(data):
    written, skipped = charts.draw(data, data / "charts")
    assert skipped == []
    assert names(written) == sorted(f"{chart}-{theme}.svg" for chart in charts.CHARTS for theme in ("light", "dark"))
    for path in written:
        assert ET.parse(path).getroot().tag == SVG
        text = path.read_text(encoding="utf-8")
        dark = path.name.endswith("-dark.svg")
        assert ("#1a1a19" in text, "#fcfcfb" in text) == (dark, not dark)  # each on its own surface


def test_the_same_tables_give_the_same_files(data, tmp_path):
    first, _ = charts.draw(data, tmp_path / "first")
    second, _ = charts.draw(data, tmp_path / "second")
    assert [p.read_bytes() for p in first] == [p.read_bytes() for p in second]


def test_conditions_with_few_comparisons_are_left_out(data):
    rows = charts.load(data)["a2_conditions"]
    assert charts.condition_rows(rows, charts.MIN_PAIRS) == [("Premium+", 3.8, -3.5), ("İyi", -6.1, -6.3)]
    assert [name for name, *_ in charts.condition_rows(rows, 1)] == ["Premium+", "Çok İyi", "İyi"]
    assert charts.reference_condition(rows) == "Mükemmel"


def test_sellers_are_counted_by_the_band_of_the_analysis(data):
    edges, counts, platform = charts.seller_bins(charts.load(data)["a3_seller_map"])
    assert platform == [-13.0]  # drawn as a line, not counted
    assert (edges[0], edges[-1], len(edges)) == (-15.0, 7.5, 10)
    assert {band: sum(c) for band, c in counts.items()} == {"below": 1, "within": 1, "above": 1}
    assert counts["below"][edges.index(-5.0)] == 1


def test_an_unknown_band_is_refused(tmp_path):
    analysis.write(tables(a3_seller_map=(SELLER_MAP, [(1, False, D("1.0"), D("1.0"), "middle")])), tmp_path, "page")
    with pytest.raises(charts.ChartError, match="unknown position bands: middle"):
        charts.draw(tmp_path, tmp_path / "charts")


def test_the_fit_line_follows_the_fitted_ratios():
    fit = {"change_per_year_pct": "-22.1", "ratio_at_1_year": "2.33", "youngest_years": "1", "oldest_years": "5"}
    ages, ratios = charts.fit_line(fit)
    assert (ages[0], ages[-1], ratios[0]) == (1, 5, 2.33)
    assert ratios[-1] == pytest.approx(0.86, abs=0.005)  # ratio_at_5_years of the same fit
    assert charts.fit_line(fit | {"change_per_year_pct": ""}) is None  # one age: no slope


def test_periods_and_percentages_read_well():
    assert charts.span(date(2026, 10, 12), date(2026, 10, 18)) == "12–18 Oct 2026"
    assert charts.span(date(2026, 10, 12), date(2026, 10, 18), year=False) == "12–18 Oct"
    assert charts.span(date(2026, 10, 29), date(2026, 11, 4)) == "29 Oct – 4 Nov 2026"
    assert charts.span(date(2026, 12, 30), date(2027, 1, 2)) == "30 Dec 2026 – 2 Jan 2027"
    assert charts.span(date(2026, 11, 4), date(2026, 11, 4)) == "4 Nov 2026"
    assert (charts.pct(-3.5), charts.pct(27.5), charts.pct(-0.04), charts.pct(-10, digits=0)) == (
        "−3.5%",
        "+27.5%",
        "0.0%",
        "−10%",
    )


def test_a_chart_without_rows_is_skipped(tmp_path):
    analysis.write(tables(a3_seller_map=(SELLER_MAP, [(1, True, D("289.4"), D("-13.0"), "below")])), tmp_path, "page")
    written, skipped = charts.draw(tmp_path, tmp_path / "charts")
    assert skipped == ["sellers"]
    assert len(written) == 6


def test_main_names_a_missing_table(tmp_path, capsys):
    assert charts.main([str(tmp_path)]) == 2
    assert "overview.csv not found" in capsys.readouterr().err


def test_main_names_a_missing_column(tmp_path, capsys):
    fit = (["models", "segments", "youngest_years", "oldest_years", "change_per_year_pct", "ratio_at_1_year"], [])
    analysis.write(tables(a1_fit=fit), tmp_path, "page")
    assert charts.main([str(tmp_path)]) == 2
    assert "a1_fit.csv has no column r2" in capsys.readouterr().err


def test_main_writes_next_to_the_tables(data, capsys):
    assert charts.main([str(data)]) == 0
    assert (data / "charts" / "supply-dark.svg").is_file()
    assert "wrote 8 charts" in capsys.readouterr().out
