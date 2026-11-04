"""Charts of the market report, drawn from the tables python -m price_radar.analysis.run writes.

    python -m price_radar.analysis.charts DATA [--out DIR] [--min-pairs N]

DATA is a directory the analyses were written to, or the downloaded ``analyses`` artifact; DIR (default DATA/charts)
gets every chart as SVG twice, for a light and for a dark page. A report shows the pair in a <picture> element, which
picks the one matching the reader's theme. The charts read the CSV files only, so they can be drawn again from any
saved run, without the database.

The colours are the categorical and diverging steps of a palette validated for colour vision deficiencies on these
two surfaces; the dark steps are chosen for the dark surface, not inverted. Text is never drawn in a series colour.
Every figure a chart shows is also in a table of the report or in the CSV files. The SVG files are reproducible: the
same tables give the same bytes.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import matplotlib
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter, MaxNLocator, NullLocator

# A condition is drawn only with at least this many comparisons with the same seller's reference offer.
MIN_PAIRS = 10
# Width of a bin of the seller histogram, in percentage points; the ±5% bands fall on bin edges.
BIN_PCT = 2.5
# One platform is collected (A4 is suspended), and the seller map flags its own store.
PLATFORM_STORE = "Getmobil's own store"
BANDS = {"below": "More than 5% below the median", "within": "Within 5%", "above": "More than 5% above"}
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
WIDTH = 7.2  # inches
HEADER = 0.78  # inches above the plot for the title and the subtitle
RC = {
    "font.family": "DejaVu Sans",  # ships with matplotlib: the same glyphs on every machine
    "font.size": 9,
    "svg.fonttype": "path",
    "svg.hashsalt": "price-radar",
    "axes.unicode_minus": True,
}
# The columns each chart reads, checked before anything is drawn.
COLUMNS = {
    "overview": ["start_day", "end_day", "window_start", "window_days"],
    "a1_age_curve": ["generation", "age_years", "price_to_us_launch"],
    "a1_fit": ["models", "segments", "change_per_year_pct", "r2", "ratio_at_1_year", "youngest_years", "oldest_years"],
    "a2_conditions": ["condition", "segment_median_pct", "within_seller_pairs", "within_seller_pct"],
    "a3_seller_map": ["is_platform", "mean_position_pct", "position_band"],
    "a6_supply_by_generation": ["generation", "first_window_offers_a_day", "last_window_offers_a_day", "change_pct"],
}

Rows = list[dict[str, str]]
Tables = Mapping[str, Rows]


class ChartError(RuntimeError):
    """A table a chart needs is missing or lacks a column."""


@dataclass(frozen=True)
class Theme:
    name: str
    surface: str
    ink: str
    ink_2: str
    muted: str
    grid: str
    axis: str
    series_1: str
    series_2: str
    below: str
    within: str
    above: str


THEMES = (
    Theme(
        "light",
        surface="#fcfcfb",
        ink="#0b0b0b",
        ink_2="#52514e",
        muted="#898781",
        grid="#e1e0d9",
        axis="#c3c2b7",
        series_1="#2a78d6",
        series_2="#eb6834",
        below="#2a78d6",
        within="#898781",
        above="#e34948",
    ),
    Theme(
        "dark",
        surface="#1a1a19",
        ink="#f0efec",
        ink_2="#c3c2b7",
        muted="#898781",
        grid="#2c2c2a",
        axis="#383835",
        series_1="#3987e5",
        series_2="#d95926",
        below="#3987e5",
        within="#b5b3ab",  # lighter than the light theme's gray: kept apart from the red under protanopia
        above="#e66767",
    ),
)


@dataclass(frozen=True)
class Period:
    start: date
    end: date
    window_start: date
    window_days: int

    @property
    def first_window_end(self) -> date:
        return min(self.start + timedelta(days=self.window_days - 1), self.end)


@dataclass(frozen=True)
class Settings:
    min_pairs: int = MIN_PAIRS


def read_table(data: Path, name: str, columns: list[str]) -> Rows:
    path = data / f"{name}.csv"
    if not path.is_file():
        raise ChartError(f"{path} not found: write the analyses first (python -m price_radar.analysis.run)")
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if missing := [column for column in columns if column not in (reader.fieldnames or [])]:
            raise ChartError(f"{path.name} has no column {', '.join(missing)}")
        return list(reader)


def load(data: Path) -> dict[str, Rows]:
    return {name: read_table(data, name, columns) for name, columns in COLUMNS.items()}


def number(row: Mapping[str, str], column: str) -> float | None:
    value = row.get(column) or ""
    return float(value) if value.strip() else None


def period_of(tables: Tables) -> Period:
    if len(tables["overview"]) != 1:
        raise ChartError("overview.csv must hold one row")
    (row,) = tables["overview"]
    return Period(
        date.fromisoformat(row["start_day"]),
        date.fromisoformat(row["end_day"]),
        date.fromisoformat(row["window_start"]),
        int(row["window_days"]),
    )


def span(start: date, end: date, *, year: bool = True) -> str:
    """12–18 Oct 2026, 29 Oct – 4 Nov 2026; the start's year only when it differs from the end's."""

    def day(d: date, with_year: bool) -> str:
        return f"{d.day} {MONTHS[d.month - 1]}" + (f" {d.year}" if with_year else "")

    if start == end:
        return day(end, year)
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.day}–{day(end, year)}"
    return f"{day(start, start.year != end.year)} – {day(end, year)}"


def pct(value: float, *, signed: bool = True, digits: int = 1) -> str:
    if signed and round(value, digits) == 0:
        return f"{0:.{digits}f}%"
    text = f"{value:+.{digits}f}%" if signed else f"{value:.{digits}f}%"
    return text.replace("-", "−")


def fit_line(fit: Mapping[str, str], steps: int = 50) -> tuple[list[float], list[float]] | None:
    """The fitted ratio over the ages of the fit: ratio at one year × (1 + change per year) ** (age − 1)."""
    change, ratio_1 = number(fit, "change_per_year_pct"), number(fit, "ratio_at_1_year")
    youngest, oldest = number(fit, "youngest_years"), number(fit, "oldest_years")
    if change is None or ratio_1 is None or youngest is None or oldest is None or oldest <= youngest:
        return None
    ages = [youngest + (oldest - youngest) * i / steps for i in range(steps + 1)]
    return ages, [ratio_1 * (1 + change / 100) ** (age - 1) for age in ages]


def condition_rows(rows: Rows, min_pairs: int) -> list[tuple[str, float, float]]:
    """(condition, within the same seller %, on segment medians %) for the conditions with enough pairs."""
    drawn = []
    for row in rows:
        pairs, within, medians = (
            number(row, c) for c in ("within_seller_pairs", "within_seller_pct", "segment_median_pct")
        )
        if pairs is not None and pairs >= min_pairs and within is not None and medians is not None:
            drawn.append((row["condition"], within, medians))
    return drawn


def reference_condition(rows: Rows) -> str:
    for row in rows:
        if number(row, "segment_median_pct") is None and number(row, "within_seller_pairs") is None:
            return row["condition"]
    return "the reference condition"


def seller_bins(rows: Rows) -> tuple[list[float], dict[str, list[int]], list[float]]:
    """Left bin edges, the count of other sellers per band and bin, and the platform store's position."""
    others = [(number(r, "mean_position_pct"), r["position_band"]) for r in rows if r["is_platform"] == "no"]
    others = [(value, band) for value, band in others if value is not None]
    platform = [v for r in rows if r["is_platform"] == "yes" and (v := number(r, "mean_position_pct")) is not None]
    if not others:
        return [], {band: [] for band in BANDS}, platform
    unknown = {band for _, band in others} - BANDS.keys()
    if unknown:
        raise ChartError(f"a3_seller_map.csv has unknown position bands: {', '.join(sorted(unknown))}")
    values = [value for value, _ in others] + platform
    low = math.floor(min(values) / BIN_PCT) * BIN_PCT
    count = math.floor((max(values) - low) / BIN_PCT) + 1
    edges = [low + BIN_PCT * i for i in range(count)]
    counts = {band: [0] * count for band in BANDS}
    for value, band in others:
        counts[band][min(math.floor((value - low) / BIN_PCT), count - 1)] += 1
    return edges, counts, platform


def figure(theme: Theme, height: float, title: str, subtitle: str) -> tuple[Figure, Axes]:
    fig = Figure(figsize=(WIDTH, height), facecolor=theme.surface, layout="constrained")
    fig.get_layout_engine().set(rect=(0, 0, 1, 1 - HEADER / height), w_pad=0.12, h_pad=0.1)
    left = 0.14 / WIDTH
    fig.text(left, 1 - 0.34 / height, title, color=theme.ink, fontsize=12, fontweight="bold")
    fig.text(left, 1 - 0.6 / height, subtitle, color=theme.ink_2, fontsize=9)
    ax = fig.add_subplot()
    ax.set_facecolor(theme.surface)
    for side in ax.spines.values():
        side.set_visible(False)
    ax.tick_params(colors=theme.ink_2, labelsize=9, length=0, pad=5)
    ax.xaxis.label.set_color(theme.ink_2)
    ax.yaxis.label.set_color(theme.ink_2)
    ax.set_axisbelow(True)
    return fig, ax


def baseline(ax: Axes, theme: Theme) -> None:
    ax.spines["bottom"].set_visible(True)
    ax.spines["bottom"].set_color(theme.axis)
    ax.spines["bottom"].set_linewidth(0.8)


def legend(ax: Axes, theme: Theme, handles: list, **kwargs) -> None:
    options = {"loc": "lower left", "bbox_to_anchor": (0, 1.01), "ncols": len(handles), "borderaxespad": 0}
    ax.legend(
        handles=handles, frameon=False, labelcolor=theme.ink_2, fontsize=9, handlelength=1.2, **(options | kwargs)
    )


def signed_ticks(ax: Axes) -> None:
    # whole steps of 1, 2, 5 or 10 points, so no tick needs rounding
    ax.xaxis.set_major_locator(MaxNLocator(nbins=8, steps=[1, 2, 5, 10], integer=True))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _: pct(value, digits=0)))


def tip_labels(ax: Axes, theme: Theme, values: list[tuple[float, float | None, str]]) -> None:
    for y, value, text in values:
        x = value or 0
        ax.annotate(
            text,
            (x, y),
            xytext=(4 if x >= 0 else -4, 0),
            textcoords="offset points",
            ha="left" if x >= 0 else "right",
            va="center",
            color=theme.ink_2,
            fontsize=8.5,
        )


def padded(values: list[float], pad: float) -> tuple[float, float]:
    return min(0, *values) - pad, max(0, *values) + pad


def depreciation(tables: Tables, period: Period, theme: Theme, _: Settings) -> Figure | None:
    points = [
        (age, ratio, row["generation"])
        for row in tables["a1_age_curve"]
        if (age := number(row, "age_years")) is not None and (ratio := number(row, "price_to_us_launch"))
    ]
    if not points:
        return None
    fig, ax = figure(
        theme,
        4.3,
        "Price against the US launch price, by age",
        f"Mükemmel condition, dollar prices, {span(period.window_start, period.end)} · one point per generation",
    )
    baseline(ax, theme)
    ax.grid(axis="y", color=theme.grid, linewidth=0.8)
    ages, ratios = [p[0] for p in points], [p[1] for p in points]
    low, high = min(ratios) / 1.35, max(ratios) * 1.35
    ax.set_yscale("log")
    ax.set_ylim(low, high)
    ticks = [2.0**k for k in range(-6, 7) if low <= 2.0**k <= high]
    ax.set_yticks(ticks, [f"{t:g}×" for t in ticks])
    ax.yaxis.set_minor_locator(NullLocator())
    ax.set_xlim(0, math.ceil(max(ages)) + 0.6)
    ax.set_xticks(range(math.ceil(max(ages)) + 1))
    ax.set_xlabel("Age in years, from the US launch")
    ax.set_ylabel("Price ÷ US launch price (log scale)")
    if low <= 1 <= high:
        ax.axhline(1, color=theme.muted, linewidth=0.8, zorder=1)
        ax.annotate(
            "US launch price",
            (ax.get_xlim()[1], 1),
            xytext=(-2, 3),
            textcoords="offset points",
            ha="right",
            va="bottom",
            color=theme.ink_2,
            fontsize=8.5,
        )
    handles = [
        Line2D(
            [],
            [],
            marker="o",
            linestyle="none",
            color=theme.series_1,
            markeredgecolor=theme.surface,
            markersize=7,
            label="Generation: mean over its segments",
        )
    ]
    fit = next((row for row in tables["a1_fit"] if row["models"] == "all models"), None)
    if fit and (line := fit_line(fit)):
        ax.plot(*line, color=theme.ink_2, linewidth=1.5, solid_capstyle="round", zorder=2)
        r2 = number(fit, "r2")
        handles.append(
            Line2D(
                [],
                [],
                color=theme.ink_2,
                linewidth=1.5,
                label=f"Fit over {fit['segments']} segments: {pct(number(fit, 'change_per_year_pct') or 0)} a year"
                + (f", R² {r2:.2f}" if r2 is not None else ""),
            )
        )
    ax.plot(
        ages,
        ratios,
        "o",
        color=theme.series_1,
        markersize=7,
        markeredgecolor=theme.surface,
        markeredgewidth=1.5,
        zorder=3,
    )
    for age, ratio, generation in points:
        ax.annotate(generation, (age, ratio), xytext=(6, 5), textcoords="offset points", color=theme.ink, fontsize=9)
    legend(ax, theme, handles, loc="upper right", bbox_to_anchor=(1, 1), ncols=1)
    return fig


def conditions(tables: Tables, period: Period, theme: Theme, settings: Settings) -> Figure | None:
    rows = condition_rows(tables["a2_conditions"], settings.min_pairs)
    if not rows:
        return None
    reference = reference_condition(tables["a2_conditions"])
    fig, ax = figure(
        theme,
        1.9 + 0.7 * len(rows),
        f"What a condition costs against {reference}",
        f"Same phone and day, {span(period.window_start, period.end)}"
        f" · conditions with at least {settings.min_pairs} comparisons within a seller",
    )
    ax.grid(axis="x", color=theme.grid, linewidth=0.8)
    ax.axvline(0, color=theme.muted, linewidth=0.8, zorder=1)
    height, offset = 0.34, 0.19
    labels = []
    for i, (_, within, medians) in enumerate(rows):
        ax.barh(i - offset, within, height=height, color=theme.series_1, zorder=2)
        ax.barh(i + offset, medians, height=height, color=theme.series_2, zorder=2)
        labels += [(i - offset, within, pct(within)), (i + offset, medians, pct(medians))]
    tip_labels(ax, theme, labels)
    ax.set_yticks(range(len(rows)), [name for name, *_ in rows], color=theme.ink, fontsize=9.5)
    ax.set_ylim(len(rows) - 0.45, -0.55)
    ax.set_xlim(*padded([v for _, a, b in rows for v in (a, b)], 3))
    signed_ticks(ax)
    ax.set_xlabel(f"Price against {reference}")
    legend(
        ax,
        theme,
        [
            Patch(color=theme.series_1, label="Within the same seller"),
            Patch(color=theme.series_2, label="Segment medians (mixes sellers)"),
        ],
    )
    return fig


def sellers(tables: Tables, period: Period, theme: Theme, _: Settings) -> Figure | None:
    edges, counts, platform = seller_bins(tables["a3_seller_map"])
    if not edges:
        return None
    total = sum(sum(c) for c in counts.values())
    fig, ax = figure(
        theme,
        4.3,
        "Where sellers price against the segment median",
        f"{total} sellers with enough comparable offers, {span(period.window_start, period.end)}",
    )
    baseline(ax, theme)
    ax.grid(axis="y", color=theme.grid, linewidth=0.8)
    colours = {"below": theme.below, "within": theme.within, "above": theme.above}
    bottom = [0] * len(edges)
    for band, colour in colours.items():
        heights = counts[band]
        ax.bar(
            edges,
            heights,
            width=BIN_PCT,
            bottom=bottom,
            align="edge",
            color=colour,
            edgecolor=theme.surface,
            linewidth=1.0,
            zorder=2,
        )
        bottom = [b + h for b, h in zip(bottom, heights, strict=True)]
    top = max(bottom)
    ax.set_ylim(0, top * 1.22 + 1)
    ax.yaxis.get_major_locator().set_params(integer=True)
    for position in platform:
        ax.axvline(position, color=theme.ink, linewidth=1.2, zorder=3)
        ax.annotate(
            f"{PLATFORM_STORE}, {pct(position)}",
            (position, ax.get_ylim()[1]),
            xytext=(4, -2),
            textcoords="offset points",
            ha="left",
            va="top",
            color=theme.ink,
            fontsize=8.5,
        )
    ax.set_xlim(edges[0] - BIN_PCT / 2, edges[-1] + BIN_PCT * 1.5)
    signed_ticks(ax)
    ax.set_xlabel("Mean price against the segment's median seller")
    ax.set_ylabel("Sellers")
    legend(
        ax, theme, [Patch(color=colours[band], label=f"{text} ({sum(counts[band])})") for band, text in BANDS.items()]
    )
    return fig


def supply(tables: Tables, period: Period, theme: Theme, _: Settings) -> Figure | None:
    rows = tables["a6_supply_by_generation"]
    if not rows:
        return None
    fig, ax = figure(
        theme,
        1.75 + 0.36 * len(rows),
        "Offers a day by generation",
        f"Change from {span(period.start, period.first_window_end, year=period.start.year != period.end.year)}"
        f" to {span(period.window_start, period.end)}",
    )
    ax.grid(axis="x", color=theme.grid, linewidth=0.8)
    ax.axvline(0, color=theme.muted, linewidth=0.8, zorder=1)
    labels, changes = [], []
    for i, row in enumerate(rows):
        change = number(row, "change_pct")
        if change is None:
            labels.append((i, None, "new" if number(row, "last_window_offers_a_day") else "–"))
            continue
        ax.barh(i, change, height=0.56, color=theme.series_1, zorder=2)
        labels.append((i, change, pct(change)))
        changes.append(change)
    tip_labels(ax, theme, labels)
    ax.set_yticks(range(len(rows)), [row["generation"] for row in rows], color=theme.ink, fontsize=9.5)
    ax.set_ylim(len(rows) - 0.4, -0.6)
    ax.set_xlim(*padded(changes or [0], 6))
    signed_ticks(ax)
    ax.set_xlabel("Change in offers a day")
    ax.set_ylabel("Generation")
    return fig


CHARTS: dict[str, Callable[[Tables, Period, Theme, Settings], Figure | None]] = {
    "depreciation": depreciation,
    "conditions": conditions,
    "sellers": sellers,
    "supply": supply,
}


def draw(data: Path, out: Path, settings: Settings | None = None) -> tuple[list[Path], list[str]]:
    """Every chart for every theme; returns the files written and the charts without rows to draw."""
    settings = settings or Settings()
    tables = load(data)
    period = period_of(tables)
    out.mkdir(parents=True, exist_ok=True)
    written, skipped = [], []
    with matplotlib.rc_context(RC):
        for name, chart in CHARTS.items():
            for theme in THEMES:
                fig = chart(tables, period, theme, settings)
                if fig is None:
                    skipped.append(name)
                    break
                path = out / f"{name}-{theme.name}.svg"
                fig.savefig(path, format="svg", facecolor=theme.surface, metadata={"Date": None})
                written.append(path)
    return written, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m price_radar.analysis.charts")
    parser.add_argument("data", type=Path, help="directory of the analysis tables (the CSV files)")
    parser.add_argument("--out", type=Path, help="output directory (default: DATA/charts)")
    parser.add_argument(
        "--min-pairs", type=int, default=MIN_PAIRS, help=f"comparisons a condition needs (default: {MIN_PAIRS})"
    )
    args = parser.parse_args(argv)
    out = args.out or args.data / "charts"
    try:
        written, skipped = draw(args.data, out, Settings(min_pairs=args.min_pairs))
    except ChartError as err:
        print(f"cannot draw the charts: {err}", file=sys.stderr)
        return 2
    for name in skipped:
        print(f"{name}: no rows to draw")
    print(f"wrote {len(written)} charts to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
