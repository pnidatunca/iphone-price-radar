"""The training set, M0 and M1, and the rolling backtest of the price model (plan 11.2-11.6)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas", reason="the model dependency group is not installed")
import numpy as np  # noqa: E402

from price_radar.model import backtest, dataset, models  # noqa: E402

START = date(2026, 10, 12)
IPHONE_13 = {
    "device_id": "iPhone 13|128",
    "model": "iPhone 13",
    "storage_gb": 128,
    "generation": 13,
    "series": "standart",
    "release_date": date(2021, 9, 24),
    "us_launch_price_usd": 799.0,
}


def offer_day(day: int, offer: str, price_tl: float, *, condition: str = "Mükemmel", usd_try: float = 50.0, **device):
    device = {**IPHONE_13, **device}
    sim_type = device.pop("sim_type", "physical")
    snapshot = START + timedelta(days=day)
    return {
        "snapshot_date": snapshot,
        "offer_key": f"getmobil:{offer}",
        **device,
        "device_age_days": (snapshot - device["release_date"]).days,
        "color": "Mavi",
        "sim_type": sim_type,
        "condition_raw": condition,
        "condition_level": 3 if condition == "İyi" else 4,
        "segment_id": f"{device['device_id']} · {condition}",
        "seller_id": offer.split(":")[-1],
        "seller_is_platform": False,
        "price_tl": price_tl,
        "usd_try": usd_try,
    }


def frame(rows: list[dict]) -> pd.DataFrame:
    return dataset.coerce(pd.DataFrame(rows))


DEVICES = [
    {**IPHONE_13},
    {**IPHONE_13, "device_id": "iPhone 13|256", "storage_gb": 256, "us_launch_price_usd": 899.0},
    {
        "device_id": "iPhone 14 Pro|128",
        "model": "iPhone 14 Pro",
        "storage_gb": 128,
        "generation": 14,
        "series": "Pro",
        "release_date": date(2022, 9, 16),
        "us_launch_price_usd": 999.0,
    },
    {
        "device_id": "iPhone 15 Pro Max|256",
        "model": "iPhone 15 Pro Max",
        "storage_gb": 256,
        "generation": 15,
        "series": "Pro Max",
        "release_date": date(2023, 9, 22),
        "us_launch_price_usd": 1199.0,
    },
]
CONDITIONS = {"Mükemmel": 1.0, "İyi": 0.92, "Premium+": 1.04}


def market(days: int = 28, seed: int = 1) -> pd.DataFrame:
    """Four sellers per device and condition, each offer away now and then, repriced every six days."""
    rng = np.random.default_rng(seed)
    rows = []
    for device in DEVICES:
        for condition, factor in CONDITIONS.items():
            for seller in range(1, 5):
                offer = f"{device['device_id']}:{condition}:{seller}"
                prices: dict[int, float] = {}
                for day in range(days):
                    if (day + seller) % 5 == 0:
                        continue
                    block = day // 6
                    if block not in prices:
                        repriced = START + timedelta(days=6 * block)
                        age = (repriced - device["release_date"]).days / 365.25
                        usd = device["us_launch_price_usd"] * 2.6 * np.exp(-0.25 * age) * factor
                        usd *= (1 + 0.01 * seller) * (1 + 0.03 * rng.standard_normal())
                        prices[block] = round(usd * (49.0 + 0.18 * block), -1)
                    usd_try = 49.0 + 0.03 * day
                    rows.append(offer_day(day, offer, prices[block], condition=condition, usd_try=usd_try, **device))
    return frame(rows)


# -- training set -------------------------------------------------------------------------------------------------


def test_a_pair_counts_once_however_long_it_is_on_sale():
    rows = [offer_day(day, "1:7", 40_000) for day in range(3)]
    rows += [offer_day(3, "1:7", 42_000, usd_try=51.0), offer_day(4, "1:7", 40_000, usd_try=51.0)]
    pairs = dataset.offer_price_pairs(frame(rows)).set_index("price_tl")

    assert len(pairs) == 2
    first = pairs.loc[40_000]
    assert (first["first_seen"].date(), first["last_seen"].date(), first["days_seen"]) == (
        START,
        START + timedelta(4),
        4,
    )
    assert first["price_usd"] == pytest.approx(800.0)  # the rate of the day it was first seen
    assert pairs.loc[42_000, "price_usd"] == pytest.approx(42_000 / 51)
    assert first["age_years"] == pytest.approx((START - IPHONE_13["release_date"]).days / 365.25)
    assert first["log_price"] == pytest.approx(np.log(800.0))


def test_an_offer_far_from_its_segment_or_its_launch_price_is_an_outlier():
    rows = [offer_day(0, f"1:{seller}", price) for seller, price in ((1, 40_000), (2, 41_000), (3, 100_000))]
    rows += [offer_day(0, f"2:{seller}", price, condition="İyi") for seller, price in ((1, 40_000), (2, 100_000))]
    rows += [
        offer_day(0, "3:1", 3_000, device_id="y", model="y"),
        offer_day(0, "4:1", 250_000, device_id="x", model="x"),
    ]
    daily = frame(rows)
    flagged = set(daily.loc[dataset.outliers(daily), "offer_key"])

    # 100,000 is 2.4 times the median of three offers; with two offers the median rule does not apply; 3,000 TL is
    # 0.08 of the launch price and 250,000 TL 6.3 times it.
    assert flagged == {"getmobil:1:3", "getmobil:3:1", "getmobil:4:1"}


def test_a_segment_is_thin_with_fewer_than_five_offers_in_the_last_seven_days():
    rows = [offer_day(0, f"1:{seller}", 40_000 + seller) for seller in range(1, 5)]
    rows += [offer_day(6, "1:9", 39_000), offer_day(7, "1:10", 39_500)]
    pairs = dataset.offer_price_pairs(frame(rows)).set_index("offer_key")

    assert pairs.loc["getmobil:1:1", "segment_offers"] == 4  # day 0: four offers
    assert pairs.loc["getmobil:1:9", "segment_offers"] == 5  # day 6: the four of day 0 and itself
    assert pairs.loc["getmobil:1:10", "segment_offers"] == 2  # day 7: day 0 is out of the window
    assert pairs["thin"].to_dict() == {
        "getmobil:1:1": True,
        "getmobil:1:2": True,
        "getmobil:1:3": True,
        "getmobil:1:4": True,
        "getmobil:1:9": False,
        "getmobil:1:10": True,
    }


def test_the_offers_need_every_column():
    with pytest.raises(dataset.DatasetError, match="seller_id"):
        dataset.coerce(pd.DataFrame([offer_day(0, "1:7", 40_000)]).drop(columns="seller_id"))


def test_the_extract_reads_in_stock_offers_of_the_period_and_no_url():
    sql, params = dataset.extract_sql(date(2026, 10, 20), None)

    assert "core.fct_offer_daily" in sql and "in_stock" in sql and "snapshot_date >= %(start)s" in sql
    assert "%(end)s" not in sql and params == {"platform": "getmobil", "start": date(2026, 10, 20)}
    assert "url" not in sql


def test_the_extract_prints_no_connection_details(monkeypatch, capsys, tmp_path):
    import psycopg

    def refuse(*args, **kwargs):
        raise psycopg.OperationalError('connection to server at "ep-secret.example" failed')

    monkeypatch.setattr("price_radar.config.database_url", lambda: "postgresql://u:p@ep-secret.example/db")
    monkeypatch.setattr(dataset, "extract", refuse)

    assert dataset.main(["extract", "--out", str(tmp_path / "x.parquet")]) == 1
    err = capsys.readouterr().err
    assert "OperationalError" in err and "secret" not in err


def test_the_extract_needs_the_database_url(monkeypatch, capsys):
    monkeypatch.setattr("price_radar.config.database_url", lambda: None)

    assert dataset.main(["extract"]) == 2
    assert "NEON_DATABASE_URL" in capsys.readouterr().err


# -- folds ----------------------------------------------------------------------------------------------------------


def test_a_fold_tests_the_pairs_first_seen_in_its_week_and_trains_on_those_before():
    pairs = dataset.offer_price_pairs(market())
    folds = backtest.make_folds(pairs, 3)

    assert [f.number for f in folds] == [1, 2]  # the third test week has 7 days before it, fewer than 10
    latest = folds[0]
    assert (latest.test_start.date(), latest.test_end.date()) == (START + timedelta(21), START + timedelta(27))
    assert latest.train["first_seen"].max() < latest.test_start
    assert latest.new["first_seen"].between(latest.test_start, latest.test_end).all()
    # Pairs first seen before the week and still on sale in it are scored with "on sale" only (K15).
    carried = latest.on_sale[latest.on_sale["first_seen"] < latest.test_start]
    assert len(carried) > 0 and carried.index.isin(latest.train.index).all()
    assert not latest.new.index.isin(latest.train.index).any()


def test_outliers_are_neither_trained_on_nor_tested():
    pairs = dataset.offer_price_pairs(market())
    pairs.loc[pairs.index[-1], "outlier"] = True
    pairs.loc[pairs.index[0], "outlier"] = True
    for fold in backtest.make_folds(pairs, 2):
        for part in (fold.train, fold.new, fold.on_sale):
            assert not part["outlier"].any()


def test_the_training_window_is_83_days():
    pairs = dataset.offer_price_pairs(market(days=100))
    latest = backtest.make_folds(pairs, 1)[0]
    window_start = latest.test_start - pd.Timedelta(days=83)

    assert latest.train["first_seen"].min() >= window_start
    assert (pairs["first_seen"] < window_start).any()


# -- models ---------------------------------------------------------------------------------------------------------


def priced(rows: list[tuple[str, str, str, int, float]]) -> pd.DataFrame:
    """Pairs with only what M0 reads: (segment, device, model, days before the cutoff, dollar price)."""
    cutoff = pd.Timestamp(START)
    return pd.DataFrame(
        {
            "segment_id": [r[0] for r in rows],
            "device_id": [r[1] for r in rows],
            "model": [r[2] for r in rows],
            "first_seen": [cutoff - pd.Timedelta(days=r[3]) for r in rows],
            "log_price": [np.log(r[4]) for r in rows],
        }
    )


def test_m0_prices_a_pair_by_its_segment_of_the_last_30_days_then_falls_back():
    train = priced(
        [
            ("13|128 · M", "13|128", "13", 40, 1000.0),
            ("13|128 · M", "13|128", "13", 5, 800.0),
            ("13|128 · M", "13|128", "13", 3, 820.0),
            ("13|128 · İ", "13|128", "13", 45, 700.0),
            ("13|256 · M", "13|256", "13", 2, 900.0),
            ("14|128 · M", "14|128", "14", 2, 1100.0),
        ]
    )
    test = priced(
        [
            ("13|128 · M", "13|128", "13", 0, 1.0),
            ("13|128 · İ", "13|128", "13", 0, 1.0),
            ("13|128 · P", "13|128", "13", 0, 1.0),
            ("13|512 · M", "13|512", "13", 0, 1.0),
            ("16|128 · M", "16|128", "16", 0, 1.0),
        ]
    )
    m0 = models.SegmentMedian().fit(train, pd.Timestamp(START))
    predicted = np.exp(m0.predict(test))

    assert predicted[:2] == pytest.approx(
        [np.sqrt(800 * 820), 700.0]
    )  # the last 30 days; else the segment's earlier pairs
    assert predicted[2] == pytest.approx(np.exp(np.median(np.log([1000, 800, 820, 700]))))  # the device
    assert predicted[3] == pytest.approx(np.exp(np.median(np.log([1000, 800, 820, 700, 900]))))  # the model
    assert predicted[4] == pytest.approx(np.exp(np.median(train["log_price"])))  # all pairs
    assert list(m0.levels_used) == ["segment", "segment, earlier", "device", "model", "all pairs"]


TRUE = {"intercept": 3.0, "age_years": -0.25, "log_launch_price": 0.9, "log2_storage": -0.05, "pro": 0.1}
TRUE_CONDITIONS = {"İyi": -0.08, "Premium+": 0.04}
TRUE_ESIM = -0.06


def hedonic_pairs(n: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    pairs = pd.DataFrame(
        {
            "age_years": rng.uniform(0.5, 8, n),
            "us_launch_price_usd": rng.choice([429.0, 699.0, 799.0, 999.0, 1199.0], n),
            "storage_gb": rng.choice([64, 128, 256, 512], n),
            "series": rng.choice(["standart", "Pro", "Pro Max", "mini"], n),
            "condition_raw": rng.choice(["Mükemmel", "İyi", "Premium+"], n),
            "sim_type": rng.choice(["physical", "esim"], n, p=[0.9, 0.1]),
        }
    )
    pairs["log_price"] = (
        TRUE["intercept"]
        + TRUE["age_years"] * pairs["age_years"]
        + TRUE["log_launch_price"] * np.log(pairs["us_launch_price_usd"])
        + TRUE["log2_storage"] * np.log2(pairs["storage_gb"])
        + TRUE["pro"] * pairs["series"].isin(["Pro", "Pro Max"])
        + pairs["condition_raw"].map(TRUE_CONDITIONS).fillna(0.0)
        + TRUE_ESIM * (pairs["sim_type"] == "esim")
    )
    return pairs


def test_m1_recovers_the_coefficients_of_a_log_linear_market():
    m1 = models.LogLinear().fit(hedonic_pairs(500, seed=3))
    coefficients = m1.coefficients().set_index("term")

    for term, value in {
        **TRUE,
        **{f"condition: {k}": v for k, v in TRUE_CONDITIONS.items()},
        "esim": TRUE_ESIM,
    }.items():
        assert coefficients.loc[term, "coefficient"] == pytest.approx(value, abs=1e-3), term
    assert coefficients.loc["age_years", "effect_pct"] == pytest.approx(100 * np.expm1(-0.25), abs=0.1)
    assert np.isnan(coefficients.loc["log_launch_price", "effect_pct"])  # an elasticity, not a multiplier


def test_m1_prices_an_unseen_condition_as_the_reference():
    m1 = models.LogLinear().fit(hedonic_pairs(300, seed=4))
    rows = hedonic_pairs(2, seed=5).assign(condition_raw=["Mükemmel", "Outlet"])
    rows["age_years"] = rows["age_years"].iloc[0]
    rows["us_launch_price_usd"] = rows["us_launch_price_usd"].iloc[0]
    rows["storage_gb"] = rows["storage_gb"].iloc[0]
    rows["series"] = rows["series"].iloc[0]
    rows["sim_type"] = "physical"

    first, second = m1.predict(rows)
    assert first == pytest.approx(second)


# -- backtest -------------------------------------------------------------------------------------------------------


def test_errors_are_percentages_of_the_price_and_lira():
    result = backtest.errors(pd.Series([100.0, 200.0]), np.array([110.0, 180.0]), pd.Series([10.0, 10.0]))

    assert result == pytest.approx({"pairs": 2, "mape_pct": 10.0, "mdape_pct": 10.0, "mae_tl": 150.0})


def test_the_backtest_scores_each_model_on_new_pairs_and_on_all_on_sale():
    result = backtest.backtest(dataset.offer_price_pairs(market()))

    assert set(result.folds["fold"]) == {1, 2}
    assert set(zip(result.pooled["model"], result.pooled["rows"], strict=True)) == {
        ("M0", "new"),
        ("M0", "on sale"),
        ("M1", "new"),
        ("M1", "on sale"),
    }
    assert set(result.breakdown["breakdown"]) == {"series", "condition", "segment"}
    assert result.fallback.groupby("fold")["share_pct"].sum().round(6).eq(100).all()
    assert "age_years" in set(result.coefficients["term"])
    # The synthetic market is log-linear in age with little noise, so both models price it closely.
    assert (result.pooled["mape_pct"] < 10).all()


def test_the_backtest_needs_ten_days_before_a_test_week():
    with pytest.raises(backtest.BacktestError, match="too little data"):
        backtest.backtest(dataset.offer_price_pairs(market(days=14)))


def test_no_output_names_a_seller_or_an_offer():
    with pytest.raises(backtest.BacktestError, match="seller_id"):
        backtest.check_columns("breakdown", pd.DataFrame(columns=["group", "seller_id"]))


def test_main_writes_the_tables_and_the_page(tmp_path: Path, capsys):
    data = tmp_path / "offer_daily.parquet"
    market().to_parquet(data, index=False)
    summary = tmp_path / "summary.md"

    assert backtest.main([str(data), "--out", str(tmp_path / "out"), "--summary", str(summary)]) == 0
    written = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert written == [
        "backtest.md",
        "breakdown.csv",
        "folds.csv",
        "m0_fallback.csv",
        "m1_coefficients.csv",
        "pooled.csv",
    ]
    for path in (tmp_path / "out").glob("*.csv"):
        header = path.read_text(encoding="utf-8").splitlines()[0].split(",")
        assert not set(header) & {"seller_id", "offer_key", "seller_key"}
    assert summary.read_text(encoding="utf-8").startswith("# Price model backtest")
    assert "wrote the backtest of 2 folds" in capsys.readouterr().out


def test_main_without_the_extract(tmp_path: Path, capsys):
    assert backtest.main([str(tmp_path / "missing.parquet")]) == 2
    assert "extract the offers first" in capsys.readouterr().err
