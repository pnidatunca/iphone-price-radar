"""The models of plan 11.5 that need no extra library: M0, the baseline, and M1, the log-linear hedonic model.

Both learn from offer-price pairs (price_radar.model.dataset) and predict the log of a pair's dollar price (plan
11.2). M2 and M2-ns, the LightGBM models, come next.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
import pandas as pd

PRO_SERIES = ("Pro", "Pro Max")
REFERENCE_CONDITION = "Mükemmel"  # the dbt var reference_condition


class Model(Protocol):
    name: str

    def fit(self, train: pd.DataFrame, cutoff: pd.Timestamp) -> Model: ...

    def predict(self, rows: pd.DataFrame) -> np.ndarray: ...


@dataclass
class SegmentMedian:
    """M0: the median log price of the segment's pairs first seen in the `window_days` days before the cutoff.

    A segment with no such pair falls back to its whole history, then to its device (model and storage, any
    condition), then to its model, then to all pairs. `levels_used` says which level priced each row of the last
    prediction.
    """

    window_days: int = 30
    name: str = "M0"
    levels: list[tuple[str, str, pd.Series]] = field(default_factory=list)
    overall: float = math.nan
    levels_used: pd.Series | None = None

    def fit(self, train: pd.DataFrame, cutoff: pd.Timestamp) -> SegmentMedian:
        recent = train[train["first_seen"] >= cutoff - pd.Timedelta(days=self.window_days)]
        self.levels = [
            ("segment", "segment_id", recent.groupby("segment_id")["log_price"].median()),
            ("segment, earlier", "segment_id", train.groupby("segment_id")["log_price"].median()),
            ("device", "device_id", train.groupby("device_id")["log_price"].median()),
            ("model", "model", train.groupby("model")["log_price"].median()),
        ]
        self.overall = float(train["log_price"].median())
        return self

    def predict(self, rows: pd.DataFrame) -> np.ndarray:
        prediction = pd.Series(np.nan, index=rows.index, dtype="float64")
        used = pd.Series("all pairs", index=rows.index, dtype="object")
        for level, key, medians in self.levels:
            open_rows = prediction.isna()
            if not open_rows.any():
                break
            found = rows.loc[open_rows, key].astype("object").map(medians).astype("float64").dropna()
            prediction.loc[found.index] = found
            used.loc[found.index] = level
        self.levels_used = used
        return prediction.fillna(self.overall).to_numpy()


@dataclass
class LogLinear:
    """M1: least squares of the log dollar price on the pair's features (plan 11.4), for coefficients one can read.

    Terms: age in years, the log of the US launch price, log2 of the storage, Pro and Pro Max models, each condition
    against Mükemmel, and eSIM. The launch price term is an elasticity: 0.9 means a launch price 10% higher goes with
    a price about 9% higher. For the others exp(coefficient) - 1 is the change in price for one more year, for double
    the storage at the same launch price, or against the reference. Dummies for every series would fit a little
    better but share what the launch price says, which turns its coefficient meaningless (negative on 12.10-7.11);
    prediction is the job of M2. A tiny ridge keeps the solution unique when two terms coincide in the data.
    """

    ridge: float = 1e-6
    name: str = "M1"
    condition_levels: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()
    coef: np.ndarray | None = None

    def design(self, rows: pd.DataFrame) -> pd.DataFrame:
        columns: dict[str, object] = {
            "intercept": 1.0,
            "age_years": rows["age_years"].to_numpy(dtype="float64"),
            "log_launch_price": np.log(rows["us_launch_price_usd"].to_numpy(dtype="float64")),
            "log2_storage": np.log2(rows["storage_gb"].to_numpy(dtype="float64")),
            "pro": rows["series"].astype("object").isin(PRO_SERIES).to_numpy(dtype="float64"),
        }
        condition = rows["condition_raw"].astype("object").to_numpy()
        for level in self.condition_levels:
            columns[f"condition: {level}"] = (condition == level).astype("float64")
        columns["esim"] = (rows["sim_type"].astype("object").to_numpy() == "esim").astype("float64")
        return pd.DataFrame(columns, index=rows.index)

    def fit(self, train: pd.DataFrame, cutoff: pd.Timestamp | None = None) -> LogLinear:
        self.condition_levels = tuple(sorted(set(train["condition_raw"].dropna()) - {REFERENCE_CONDITION}))
        x = self.design(train)
        self.terms = tuple(x.columns)
        x_values = x.to_numpy()
        penalty = np.eye(len(self.terms)) * self.ridge * len(train)
        penalty[0, 0] = 0.0  # the intercept is not shrunk
        self.coef = np.linalg.solve(x_values.T @ x_values + penalty, x_values.T @ train["log_price"].to_numpy())
        return self

    def predict(self, rows: pd.DataFrame) -> np.ndarray:
        if self.coef is None:
            raise RuntimeError("fit the model first")
        return self.design(rows).to_numpy() @ self.coef

    def coefficients(self) -> pd.DataFrame:
        if self.coef is None:
            raise RuntimeError("fit the model first")
        frame = pd.DataFrame({"term": self.terms, "coefficient": self.coef})
        multiplicative = ~frame["term"].isin(["intercept", "log_launch_price"])
        frame["effect_pct"] = np.where(multiplicative, 100 * np.expm1(frame["coefficient"]), np.nan)
        return frame


MODELS: dict[str, type] = {"M0": SegmentMedian, "M1": LogLinear}
