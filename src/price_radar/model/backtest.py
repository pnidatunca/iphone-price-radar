"""The rolling backtest of plan 11.6: train on the weeks before, predict the next week, and repeat a week earlier.

    python -m price_radar.model.backtest [DATA] [--folds N] [--out DIR] [--summary FILE]

DATA is the file python -m price_radar.model.dataset extract wrote (default data/model/offer_daily.parquet). Each
fold trains on the pairs first seen in the 83 days before its test week (plan 11.3), at least 10 days of them. Its
test week is scored twice: on the pairs first seen in it ("new", the measure that counts, K15), and on every pair on
sale in it ("on sale"), which includes pairs the model trained on. A large gap between the two means the model
memorises offers. Outliers are left out of training and testing (plan 11.3).

DIR (default data/model/backtest) gets the errors by fold, pooled over the folds and broken down by series,
condition and segment density, M0's fallback levels and M1's coefficients, as CSV files and one Markdown page. They
hold aggregates only: a table with a column that identifies a seller or an offer is refused (K22).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from price_radar.analysis.run import IDENTIFYING
from price_radar.model import dataset
from price_radar.model.models import MODELS, LogLinear, SegmentMedian

DEFAULT_OUT = Path("data/model/backtest")
FOLDS = 3
TEST_DAYS = 7
TRAIN_DAYS = 83  # the 90-day window less the test week
MIN_TRAIN_DAYS = 10  # more than a week, so most segments are seen (98.7% of new pairs after 13 days, 25-31.10)
NEW, ON_SALE = "new", "on sale"
BREAKDOWNS = {"series": "series", "condition": "condition_raw", "segment": "density"}


class BacktestError(RuntimeError):
    """Too little data for one fold, or an output that must not be written."""


@dataclass(frozen=True)
class Fold:
    number: int  # 1 is the latest test week
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    train: pd.DataFrame
    new: pd.DataFrame
    on_sale: pd.DataFrame


def make_folds(
    pairs: pd.DataFrame,
    folds: int = FOLDS,
    *,
    test_days: int = TEST_DAYS,
    train_days: int = TRAIN_DAYS,
    min_train_days: int = MIN_TRAIN_DAYS,
) -> list[Fold]:
    """The latest `folds` test weeks that have at least `min_train_days` days before them."""
    clean = pairs[~pairs["outlier"]]
    first_day, last_day = pairs["first_seen"].min(), pairs["last_seen"].max()
    result = []
    for number in range(1, folds + 1):
        test_end = last_day - pd.Timedelta(days=(number - 1) * test_days)
        test_start = test_end - pd.Timedelta(days=test_days - 1)
        if (test_start - first_day).days < min_train_days:
            break
        train_start = max(first_day, test_start - pd.Timedelta(days=train_days))
        seen = clean["first_seen"]
        result.append(
            Fold(
                number=number,
                test_start=test_start,
                test_end=test_end,
                train=clean[(seen >= train_start) & (seen < test_start)],
                new=clean[(seen >= test_start) & (seen <= test_end)],
                on_sale=clean[(seen <= test_end) & (clean["last_seen"] >= test_start)],
            )
        )
    return result


def errors(actual_usd: pd.Series, predicted_usd: np.ndarray, usd_try: pd.Series) -> dict[str, float]:
    """MAPE, the median absolute percentage error and the mean absolute error in lira (plan 11.6)."""
    actual = actual_usd.to_numpy(dtype="float64")
    gap = np.abs(np.asarray(predicted_usd, dtype="float64") - actual)
    ape = gap / actual
    return {
        "pairs": len(actual),
        "mape_pct": 100 * float(ape.mean()),
        "mdape_pct": 100 * float(np.median(ape)),
        "mae_tl": float((gap * usd_try.to_numpy(dtype="float64")).mean()),
    }


@dataclass
class Result:
    folds: pd.DataFrame
    pooled: pd.DataFrame
    breakdown: pd.DataFrame
    fallback: pd.DataFrame
    coefficients: pd.DataFrame

    def tables(self) -> dict[str, pd.DataFrame]:
        return {
            "pooled": self.pooled,
            "folds": self.folds,
            "breakdown": self.breakdown,
            "m0_fallback": self.fallback,
            "m1_coefficients": self.coefficients,
        }


def backtest(pairs: pd.DataFrame, models: Sequence[str] = tuple(MODELS), folds: int = FOLDS, **fold_args) -> Result:
    made = make_folds(pairs, folds, **fold_args)
    if not made:
        raise BacktestError(
            f"too little data: a fold needs {fold_args.get('min_train_days', MIN_TRAIN_DAYS)} days before a test week"
        )
    by_fold, scored, fallback = [], [], []
    coefficients = pd.DataFrame()
    for fold in made:
        for name in models:
            model = MODELS[name]().fit(fold.train, fold.test_start)
            for rows, test in ((NEW, fold.new), (ON_SALE, fold.on_sale)):
                if test.empty:
                    continue
                predicted = np.exp(model.predict(test))
                by_fold.append(
                    {
                        "fold": fold.number,
                        "test_start": fold.test_start.date(),
                        "test_end": fold.test_end.date(),
                        "train_pairs": len(fold.train),
                        "model": name,
                        "rows": rows,
                        **errors(test["price_usd"], predicted, test["usd_try"]),
                    }
                )
                kept = test[["series", "condition_raw", "thin", "price_usd", "usd_try"]].copy()
                scored.append(kept.assign(fold=fold.number, model=name, rows=rows, predicted_usd=predicted))
                if rows == NEW and isinstance(model, SegmentMedian) and model.levels_used is not None:
                    counts = model.levels_used.value_counts()
                    fallback.extend(
                        {"fold": fold.number, "level": level, "pairs": int(n), "share_pct": 100 * n / len(test)}
                        for level, n in counts.items()
                    )
            if fold.number == made[0].number and isinstance(model, LogLinear):
                coefficients = model.coefficients()
    all_scored = pd.concat(scored, ignore_index=True)
    all_scored["density"] = np.where(all_scored["thin"].fillna(True), "thin", "dense")
    return Result(
        folds=pd.DataFrame(by_fold),
        pooled=_grouped(all_scored, ["model", "rows"]),
        breakdown=_breakdown(all_scored[all_scored["rows"] == NEW]),
        fallback=pd.DataFrame(fallback, columns=["fold", "level", "pairs", "share_pct"]),
        coefficients=coefficients,
    )


def _grouped(scored: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    records = []
    for values, group in scored.groupby(keys, sort=True):
        key_values = values if isinstance(values, tuple) else (values,)
        records.append(
            {
                **dict(zip(keys, key_values, strict=True)),
                **errors(group["price_usd"], group["predicted_usd"], group["usd_try"]),
            }
        )
    return pd.DataFrame(records)


def _breakdown(scored: pd.DataFrame) -> pd.DataFrame:
    frames = []
    for breakdown, column in BREAKDOWNS.items():
        frame = _grouped(scored, [column, "model"]).rename(columns={column: "group"})
        frames.append(frame.assign(breakdown=breakdown))
    columns = ["breakdown", "group", "model", "pairs", "mape_pct", "mdape_pct", "mae_tl"]
    return pd.concat(frames, ignore_index=True)[columns]


def check_columns(name: str, frame: pd.DataFrame) -> None:
    if found := sorted(IDENTIFYING & set(frame.columns)):
        raise BacktestError(f"{name} has {', '.join(found)}, which identifies sellers or offers")


ROUNDING = {"mape_pct": 1, "mdape_pct": 1, "share_pct": 1, "mae_tl": 0, "coefficient": 4, "effect_pct": 1}


def rounded(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    for column, digits in ROUNDING.items():
        if column in frame.columns:
            frame[column] = frame[column].round(digits)
            if digits == 0:
                frame[column] = frame[column].astype("Int64")
    return frame


def table_markdown(frame: pd.DataFrame) -> str:
    frame = rounded(frame)
    head = "| " + " | ".join(frame.columns) + " |"
    rule = "|" + "---|" * len(frame.columns)
    body = [
        "| " + " | ".join("" if pd.isna(v) else f"{v:,}" if isinstance(v, int) else str(v) for v in row) + " |"
        for row in frame.itertuples(index=False)
    ]
    return "\n".join([head, rule, *body])


def markdown(result: Result, pairs: pd.DataFrame) -> str:
    sections = [
        "# Price model backtest",
        "",
        *[f"- {line}" for line in dataset.summary(pairs)],
        "- Errors on the dollar price; MAE in lira at each pair's rate.",
        "- `new`: pairs first seen in the test week (K15); `on sale`: every pair on sale in it.",
        "",
    ]
    titles = {
        "pooled": "Pooled over the folds",
        "folds": "By fold",
        "breakdown": "New pairs by series, condition and segment density",
        "m0_fallback": "M0: level that priced each new pair",
        "m1_coefficients": "M1: coefficients (latest fold)",
    }
    for name, frame in result.tables().items():
        if not frame.empty:
            sections += [f"## {titles[name]}", "", table_markdown(frame), ""]
    return "\n".join(sections)


def write(result: Result, page: str, out: Path) -> list[Path]:
    tables = result.tables()
    for name, frame in tables.items():
        check_columns(name, frame)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, frame in tables.items():
        path = out / f"{name}.csv"
        rounded(frame).to_csv(path, index=False)
        written.append(path)
    page_path = out / "backtest.md"
    page_path.write_text(page, encoding="utf-8")
    return [*written, page_path]


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m price_radar.model.backtest", description=__doc__.split("\n")[0])
    parser.add_argument("data", nargs="?", type=Path, default=dataset.DEFAULT_OUT)
    parser.add_argument("--folds", type=int, default=FOLDS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--summary", type=Path, help="also append the page to this file (the job summary)")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        pairs = dataset.offer_price_pairs(dataset.load(args.data))
        result = backtest(pairs, folds=args.folds)
        page = markdown(result, pairs)
        write(result, page, args.out)
    except (dataset.DatasetError, BacktestError) as err:
        print(err, file=sys.stderr)
        return 2
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as handle:
            handle.write(page + "\n")
    print(table_markdown(result.pooled))
    print(f"wrote the backtest of {len(result.folds['fold'].unique())} folds to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
