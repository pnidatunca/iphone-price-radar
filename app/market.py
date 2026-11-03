"""The Market page (plan 13, K14): the public live view of the analytics layer.

It reads four marts with the read-only database role (RADAR_READER_URL, a Streamlit secret): marts.daily_market for
the headline figures, marts.price_index for the index trend, and the last week of marts.daily_segment_prices for the
depreciation curve (A1) and of marts.daily_condition_premiums for the condition differences (A2). One cached load
serves every visitor for two hours, so visits do not keep the database awake; the marts change once a day. Only
aggregates are shown, no offers or sellers (K12, K22).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta

import altair as alt
import pandas as pd
import streamlit as st

PLATFORM = "getmobil"
REFERENCE_CONDITION = "Mükemmel"
CACHE_TTL = timedelta(hours=2)
QUERIES = {
    "market": """
        select snapshot_date, offer_count, seller_count, model_count, median_price_tl::float8 as median_price_tl,
               median_price_usd::float8 as median_price_usd, usd_try::float8 as usd_try, fx_confirmed
        from marts.daily_market
        where platform = %(platform)s
        order by snapshot_date
    """,
    "index": """
        select snapshot_date, index_tl::float8 as index_tl, index_usd::float8 as index_usd, segments_priced,
               basket_segments, weight_priced::float8 as weight_priced, basket_final, base_start, base_end
        from marts.price_index
        where platform = %(platform)s
        order by snapshot_date
    """,
    "conditions": """
        select snapshot_date, condition_raw, pairs, sellers, median_difference_pct::float8 as median_difference_pct
        from marts.daily_condition_premiums
        where platform = %(platform)s and seller_group = 'all'
            and snapshot_date > (select max(snapshot_date) - 7 from marts.daily_condition_premiums)
    """,
    "segments": """
        select snapshot_date, model, storage_gb, segment_condition, release_date, device_age_days,
               us_launch_price_usd::float8 as us_launch_price_usd, offer_count,
               median_price_tl::float8 as median_price_tl, median_price_usd::float8 as median_price_usd
        from marts.daily_segment_prices
        where platform = %(platform)s
            and snapshot_date > (select max(snapshot_date) - 7 from marts.daily_segment_prices)
    """,
}
# Two series of the dataviz reference palette (blue, orange), stepped per theme.
SERIES = {"light": ("#2a78d6", "#eb6834"), "dark": ("#3987e5", "#d95926")}
INK = {"light": "#52514e", "dark": "#c3c2b7"}


class DataUnavailable(RuntimeError):
    """The marts could not be read (no secret, no connection, no data yet)."""


def reader_url() -> str | None:
    try:
        url = st.secrets.get("RADAR_READER_URL")
    except FileNotFoundError:  # no secrets file at all (a local run)
        url = None
    return url or os.environ.get("RADAR_READER_URL")


@st.cache_data(ttl=CACHE_TTL, show_spinner="Reading the marts…")
def load_marts() -> dict[str, pd.DataFrame]:
    import psycopg  # imported here so the page renders its error without a driver

    url = reader_url()
    if not url:
        raise DataUnavailable("RADAR_READER_URL is not set")
    try:
        with psycopg.connect(url, connect_timeout=20) as conn:
            frames = {}
            for name, sql in QUERIES.items():
                cursor = conn.execute(sql, {"platform": PLATFORM})
                columns = [column.name for column in cursor.description]
                frames[name] = pd.DataFrame(cursor.fetchall(), columns=columns)
    except psycopg.Error as err:
        raise DataUnavailable(type(err).__name__) from None  # the message may name the host
    return frames


@dataclass(frozen=True)
class Headline:
    day: date
    offers: int
    offers_change: int | None  # against 7 days earlier
    sellers: int
    models: int
    median_tl: float
    median_usd: float
    usd_try: float
    fx_confirmed: bool
    index_tl: float | None
    index_tl_change: float | None  # percent over 7 days
    index_usd: float | None
    index_usd_change: float | None


def _week_before(frame: pd.DataFrame, day: date, column: str) -> float | None:
    earlier = frame[frame["snapshot_date"] <= day - timedelta(days=7)]
    return None if earlier.empty else float(earlier.iloc[-1][column])


def headline(market: pd.DataFrame, index: pd.DataFrame) -> Headline:
    latest = market.iloc[-1]
    day = latest["snapshot_date"]
    offers_before = _week_before(market, day, "offer_count")
    figures = {"index_tl": None, "index_tl_change": None, "index_usd": None, "index_usd_change": None}
    if not index.empty:
        last = index.iloc[-1]
        for currency in ("tl", "usd"):
            value = float(last[f"index_{currency}"])
            before = _week_before(index, last["snapshot_date"], f"index_{currency}")
            figures[f"index_{currency}"] = value
            figures[f"index_{currency}_change"] = None if before is None else (value / before - 1) * 100
    return Headline(
        day=day,
        offers=int(latest["offer_count"]),
        offers_change=None if offers_before is None else int(latest["offer_count"] - offers_before),
        sellers=int(latest["seller_count"]),
        models=int(latest["model_count"]),
        median_tl=float(latest["median_price_tl"]),
        median_usd=float(latest["median_price_usd"]),
        usd_try=float(latest["usd_try"]),
        fx_confirmed=bool(latest["fx_confirmed"]),
        **figures,
    )


def depreciation(segments: pd.DataFrame) -> pd.DataFrame:
    """Per model: the dollar price of the reference condition as a share of the US launch price, against the model's
    age (A1, cross-section). Each storage size's 7-day mean of daily medians, averaged over sizes by offers."""
    reference = segments[segments["segment_condition"] == REFERENCE_CONDITION].dropna(subset=["us_launch_price_usd"])
    if reference.empty:
        return pd.DataFrame(columns=["model", "age_years", "price_to_launch", "price_usd", "offers"])
    sizes = reference.groupby(["model", "storage_gb"], as_index=False).agg(
        price_usd=("median_price_usd", "mean"),
        launch_usd=("us_launch_price_usd", "first"),
        offers=("offer_count", "sum"),
        age_days=("device_age_days", "max"),
    )
    sizes["ratio"] = sizes["price_usd"] / sizes["launch_usd"]
    sizes["weighted_ratio"] = sizes["ratio"] * sizes["offers"]
    sizes["weighted_price"] = sizes["price_usd"] * sizes["offers"]
    models = sizes.groupby("model", as_index=False).agg(
        weighted_ratio=("weighted_ratio", "sum"),
        weighted_price=("weighted_price", "sum"),
        offers=("offers", "sum"),
        age_days=("age_days", "max"),
    )
    models["price_to_launch"] = models["weighted_ratio"] / models["offers"]
    models["price_usd"] = models["weighted_price"] / models["offers"]
    models["age_years"] = models["age_days"] / 365.25
    return models[["model", "age_years", "price_to_launch", "price_usd", "offers"]].sort_values("age_years")


def condition_differences(conditions: pd.DataFrame) -> pd.DataFrame:
    """Per condition: the median over the last days of the daily median difference a seller asks against its own
    offer of the reference condition for the same model and storage (A2)."""
    if conditions.empty:
        return pd.DataFrame(columns=["condition", "difference_pct", "pairs"])
    result = conditions.groupby("condition_raw", as_index=False).agg(
        difference_pct=("median_difference_pct", "median"), pairs=("pairs", "sum")
    )
    return result.rename(columns={"condition_raw": "condition"}).sort_values("difference_pct", ascending=False)


def theme() -> str:
    kind = getattr(getattr(st.context, "theme", None), "type", None)
    return "dark" if kind == "dark" else "light"


def index_chart(index: pd.DataFrame, mode: str) -> alt.LayerChart:
    long = index.melt(
        id_vars="snapshot_date", value_vars=["index_tl", "index_usd"], var_name="series", value_name="index"
    )
    long["series"] = long["series"].map({"index_tl": "Lira (TL)", "index_usd": "Dollar (USD)"})
    long["snapshot_date"] = pd.to_datetime(long["snapshot_date"])
    color = alt.Color(
        "series:N",
        scale=alt.Scale(domain=["Lira (TL)", "Dollar (USD)"], range=list(SERIES[mode])),
        legend=alt.Legend(title=None, orient="top"),
    )
    base = alt.Chart(long).encode(x=alt.X("snapshot_date:T", title=None, axis=alt.Axis(format="%d.%m", grid=False)))
    lines = base.mark_line(strokeWidth=2).encode(
        y=alt.Y("index:Q", title="Index (base period = 100)", scale=alt.Scale(zero=False)), color=color
    )
    hover = alt.selection_point(fields=["snapshot_date"], nearest=True, on="pointerover", empty=False)
    points = (
        base.mark_point(size=60, filled=True)
        .encode(
            y="index:Q",
            color=color,
            opacity=alt.condition(hover, alt.value(1), alt.value(0)),
            tooltip=[
                alt.Tooltip("snapshot_date:T", title="Day", format="%d.%m.%Y"),
                alt.Tooltip("series:N", title="Index"),
                alt.Tooltip("index:Q", title="Value", format=".1f"),
            ],
        )
        .add_params(hover)
    )
    rule = base.mark_rule(color=INK[mode], strokeWidth=1).encode(
        opacity=alt.condition(hover, alt.value(0.4), alt.value(0))
    )
    baseline = alt.Chart(pd.DataFrame({"y": [100]})).mark_rule(strokeDash=[4, 4], color=INK[mode]).encode(y="y:Q")
    return alt.layer(baseline, lines, rule, points).properties(height=320)


def depreciation_chart(models: pd.DataFrame, mode: str) -> alt.Chart:
    return (
        alt.Chart(models)
        .mark_circle(size=90, color=SERIES[mode][0], opacity=0.9)
        .encode(
            x=alt.X("age_years:Q", title="Age since US release (years)"),
            y=alt.Y("price_to_launch:Q", title="Dollar price ÷ US launch price", axis=alt.Axis(format="%")),
            tooltip=[
                alt.Tooltip("model:N", title="Model"),
                alt.Tooltip("age_years:Q", title="Age (years)", format=".1f"),
                alt.Tooltip("price_to_launch:Q", title="Of launch price", format=".0%"),
                alt.Tooltip("price_usd:Q", title="Median price (USD)", format=",.0f"),
                alt.Tooltip("offers:Q", title="Offers (7 days)", format=","),
            ],
        )
        .properties(height=340)
    )


def condition_chart(differences: pd.DataFrame, mode: str) -> alt.LayerChart:
    bars = (
        alt.Chart(differences)
        .mark_bar(color=SERIES[mode][0], cornerRadiusEnd=4, height=18)
        .encode(
            y=alt.Y("condition:N", title=None, sort=None),
            x=alt.X("difference_pct:Q", title=f"Median difference to {REFERENCE_CONDITION} (%)"),
            tooltip=[
                alt.Tooltip("condition:N", title="Condition"),
                alt.Tooltip("difference_pct:Q", title="Difference (%)", format="+.1f"),
                alt.Tooltip("pairs:Q", title="Comparisons", format=","),
            ],
        )
    )
    zero = alt.Chart(pd.DataFrame({"x": [0]})).mark_rule(color=INK[mode]).encode(x="x:Q")
    return alt.layer(bars, zero).properties(height=40 * max(len(differences), 1) + 40)


def _signed(value: float | None, unit: str = "", digits: int = 0) -> str | None:
    if value is None:
        return None
    rounded = round(value, digits) + 0.0  # no "-0.0"
    return f"{rounded:+,.{digits}f}{unit}"


def render() -> None:
    st.title("Refurbished iPhone market in Turkey")
    st.caption(
        "Daily prices of every seller's offers on Getmobil, a refurbished-device marketplace (iPhone X and newer), "
        "converted with the official TCMB rate. Updated once a day; aggregates only."
    )
    try:
        frames = load_marts()
    except DataUnavailable as err:
        st.error(f"The market data cannot be read right now ({err}).")
        return
    market, index, segments = frames["market"], frames["index"], frames["segments"]
    if market.empty:
        st.info("No market data yet.")
        return
    mode = theme()
    top = headline(market, index)

    cards = st.columns(4)
    cards[0].metric(
        "Offers in stock", f"{top.offers:,}", _signed(top.offers_change), help="Change against 7 days earlier"
    )
    cards[1].metric("Median price", f"{top.median_tl:,.0f} TL", help="Over all offers; moves with the mix of models")
    cards[2].metric("Median price in dollars", f"${top.median_usd:,.0f}", help=f"At {top.usd_try:.4f} TL per dollar")
    if top.index_tl is not None:
        cards[3].metric(
            "Price index (TL)",
            f"{top.index_tl:.1f}",
            _signed(top.index_tl_change, "%", 1),
            delta_color="off",
            help="Change over 7 days",
        )
    st.caption(
        f"Data of {top.day:%d.%m.%Y}: {top.sellers} sellers, {top.models} models."
        + (
            ""
            if top.fx_confirmed
            else " Dollar figures use the latest TCMB rate; it is confirmed by the next bulletin."
        )
    )

    st.subheader("Price index")
    if index.empty:
        st.info("The index starts once its base period is complete.")
    else:
        first = index.iloc[0]
        st.altair_chart(
            index_chart(index, mode), width="stretch", alt="Line chart of the price index in lira and in dollars"
        )
        st.caption(
            f"Fixed basket of {int(first['basket_segments'])} segments (model, storage and condition), weighted by "
            f"their offers in the base period {first['base_start']:%d.%m}–{first['base_end']:%d.%m.%Y} = 100; days "
            "are chain linked over the segments priced on both days. Not age adjusted: the devices in the basket "
            "age, so the index also falls with depreciation."
            + ("" if bool(index.iloc[-1]["basket_final"]) else " The base period is still being completed.")
        )

    left, right = st.columns(2)
    with left:
        st.subheader("Depreciation by model age")
        models = depreciation(segments)
        if models.empty:
            st.info("No prices of the reference condition this week.")
        else:
            st.altair_chart(
                depreciation_chart(models, mode),
                width="stretch",
                alt="Scatter chart of each model's dollar price as a share of its US launch price against its age",
            )
            st.caption(
                f"One dot per model: its {REFERENCE_CONDITION} offers of the last 7 days in dollars, as a share of "
                "the US launch price. Turkish prices include taxes the US price does not, so the share compares "
                "models with each other; it is not a loss of value."
            )
    with right:
        st.subheader("Price by condition")
        differences = condition_differences(frames["conditions"])
        if differences.empty:
            st.info("No condition can be compared this week.")
        else:
            st.altair_chart(
                condition_chart(differences, mode),
                width="stretch",
                alt="Bar chart of the median price difference of each condition to Mükemmel",
            )
            st.caption(
                f"What a seller asks for the condition against its own {REFERENCE_CONDITION} offer of the same model "
                "and storage on the same day; median over the last 7 days. Comparing within a seller keeps the seller "
                "mix out."
            )
    with st.expander("Table view"):
        st.dataframe(
            index.rename(columns={"snapshot_date": "day"})[["day", "index_tl", "index_usd", "segments_priced"]],
            hide_index=True,
        )
        st.dataframe(models, hide_index=True)
        st.dataframe(differences, hide_index=True)
