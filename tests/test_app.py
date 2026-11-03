"""The Streamlit Market page with synthetic marts: the figures it derives and the page itself (streamlit.testing)."""

import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("streamlit", reason="the app dependency group is not installed")
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
import market  # noqa: E402

DAYS = [date(2026, 10, 20) + timedelta(days=n) for n in range(10)]


def frames():
    market_rows = pd.DataFrame(
        {
            "snapshot_date": DAYS,
            "offer_count": [1500 + 10 * n for n in range(10)],
            "seller_count": 250,
            "model_count": 34,
            "median_price_tl": 58000.0,
            "median_price_usd": 1170.0,
            "usd_try": 49.5,
            "fx_confirmed": [True] * 9 + [False],
        }
    )
    index = pd.DataFrame(
        {
            "snapshot_date": DAYS,
            "index_tl": [100.0 + n for n in range(10)],
            "index_usd": [100.0 - n / 2 for n in range(10)],
            "segments_priced": 150,
            "basket_segments": 154,
            "weight_priced": 1.0,
            "basket_final": True,
            "base_start": DAYS[0],
            "base_end": DAYS[5],
        }
    )
    segments = pd.DataFrame(
        [
            # model, storage, condition, age, launch, offers, median usd
            ("iPhone 13", 128, "Mükemmel", 1800, 799.0, 30, 700.0),
            ("iPhone 13", 256, "Mükemmel", 1800, 899.0, 10, 900.0),
            ("iPhone 13", 128, "İyi", 1800, 799.0, 5, 600.0),
            ("iPhone 15 Pro", 128, "Mükemmel", 1100, 999.0, 20, 1500.0),
        ],
        columns=[
            "model",
            "storage_gb",
            "segment_condition",
            "device_age_days",
            "us_launch_price_usd",
            "offer_count",
            "median_price_usd",
        ],
    ).assign(snapshot_date=DAYS[-1], median_price_tl=lambda f: f["median_price_usd"] * 49.5, release_date=DAYS[0])
    conditions = pd.DataFrame(
        {
            "snapshot_date": [DAYS[-2], DAYS[-1], DAYS[-2], DAYS[-1]],
            "condition_raw": ["Premium+", "Premium+", "İyi", "İyi"],
            "pairs": [10, 12, 8, 9],
            "sellers": [5, 6, 4, 4],
            "median_difference_pct": [4.0, 5.0, -6.0, -5.0],
        }
    )
    return {"market": market_rows, "index": index, "segments": segments, "conditions": conditions}


def test_the_headline_compares_with_a_week_before():
    data = frames()
    top = market.headline(data["market"], data["index"])
    assert (top.day, top.offers, top.offers_change, top.fx_confirmed) == (DAYS[-1], 1590, 70, False)
    assert round(top.index_tl_change, 2) == round((109 / 102 - 1) * 100, 2)
    assert market.headline(data["market"].iloc[:3], data["index"].iloc[:0]).index_tl is None


def test_depreciation_weights_the_sizes_by_offers():
    models = market.depreciation(frames()["segments"]).set_index("model")
    # iPhone 13: (700/799 * 30 + 900/899 * 10) / 40; İyi is not the reference condition
    assert models.loc["iPhone 13", "price_to_launch"] == pytest.approx((700 / 799 * 30 + 900 / 899 * 10) / 40)
    assert models.loc["iPhone 13", "offers"] == 40
    assert list(models.index) == ["iPhone 15 Pro", "iPhone 13"]  # youngest first


def test_condition_differences_take_the_median_over_the_days():
    differences = market.condition_differences(frames()["conditions"])
    assert differences.to_dict("records") == [
        {"condition": "Premium+", "difference_pct": 4.5, "pairs": 22},
        {"condition": "İyi", "difference_pct": -5.5, "pairs": 17},
    ]
    assert market.condition_differences(frames()["conditions"].iloc[:0]).empty


def test_signed_figures_never_show_minus_zero():
    assert market._signed(-0.04, "%", 1) == "+0.0%"
    assert market._signed(1234, digits=0) == "+1,234"
    assert market._signed(None) is None


def run_page(monkeypatch, load):
    monkeypatch.setattr(market, "load_marts", load)
    return AppTest.from_file(str(APP / "streamlit_app.py"), default_timeout=60).run()


def test_the_page_shows_the_figures_and_charts(monkeypatch):
    page = run_page(monkeypatch, frames)
    assert not page.exception
    assert [m.label for m in page.metric] == [
        "Offers in stock",
        "Median price",
        "Median price in dollars",
        "Price index (TL)",
    ]
    assert page.metric[0].value == "1,590"
    captions = " ".join(c.value for c in page.caption)
    assert "Data of 29.10.2026" in captions and "confirmed by the next bulletin" in captions
    assert len(page.get("vega_lite_chart")) == 3


def test_the_page_explains_when_the_data_cannot_be_read(monkeypatch):
    def unavailable():
        raise market.DataUnavailable("RADAR_READER_URL is not set")

    page = run_page(monkeypatch, unavailable)
    assert not page.exception
    assert "cannot be read right now (RADAR_READER_URL is not set)" in page.error[0].value
