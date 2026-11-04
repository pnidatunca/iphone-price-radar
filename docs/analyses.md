# Analyses

The analyses of plan 10.1 that the data can answer so far (A1–A3, A6) and the headline figures of a market report
are SQL files in [`dbt/analyses/`](../dbt/analyses). They read the marts only, so they can be run for any period the
marts hold, also after the raw detail is deleted. dbt compiles them (they appear in the dbt documentation with their
lineage) and never runs them; `python -m price_radar.analysis.run` runs the compiled queries in a read-only
transaction and writes one CSV per analysis and all of them in `analyses.md`.

## Running them

On Neon: run the **analysis** workflow by hand (Actions → analysis → Run workflow). `end` is the last day of the
period (empty: the latest day built) and `days` the cross-sectional window (default 7). The tables appear in the job
summary and as the `analyses` artifact (CSV files, kept 14 days).

Locally, against any database the dbt project was built on:

```bash
uv run python -m price_radar.analysis.run --end 2026-11-14 --out reports/data/2026-11
```

The output holds aggregates only. A column that identifies a seller or an offer (`seller_key`, `offer_key`, URLs) is
refused before anything is written, and the seller map ranks sellers by size instead of naming them (K22).

## Period and window

Each analysis covers the period from the first day built to `end`. Cross-sectional figures use its last `days` days
(the window); comparisons of the start and the end use the first `days` days as well. When the period is shorter than
two windows, the two overlap. The vars are in `dbt/dbt_project.yml` (`analysis_*`).

## What each one measures

| Analysis | Method | Read with care |
|---|---|---|
| `overview` | Offers and sellers a day, the platform's share, the median price, the TCMB rate at the start and the end, the price index on the last day and its change over the window | The median price moves with the mix of models on sale; the index does not. The index is 100 over its base period (20.10–2.11) and not age adjusted |
| `a1_age_curve` | Per generation, the offer-weighted mean of each Mükemmel segment's dollar price (median of its daily medians) over its US launch price, and the change from one generation newer | Cross-sectional: models of different ages on the same days, not one model ageing. US prices exclude Turkish taxes, so a ratio above 1 is normal; it compares models, it is not a loss of value in itself |
| `a1_fit` | Least squares of ln(price / US launch price) on age in years over the same segments; `exp(slope) − 1` is the change per year of age. Also for Pro models and the others apart | Same as above; the fit says nothing about models younger or older than the data |
| `a1_storage_steps` | For each model, Apple's launch price step to the next storage size against the step between the refurbished prices, and the share of Apple's step kept; the last row is the median | Segment medians mix sellers: a single step can be negative because different sellers sell the two sizes. Read the median, not single rows |
| `a2_conditions` | What a condition costs against Mükemmel, on segment medians (same device and day) and within the same seller (`daily_condition_premiums`), for all sellers, the platform's own store and the others | Where the two disagree, the platform's share of the condition explains it: segment medians compare sellers as much as conditions. The within-seller figure is the one to quote |
| `a3_spread` | By the number of sellers in a segment: the interquartile range and the full range of the day's offers over their median, how far below the median the cheapest offer is, and the saving in lira | Offer level: a seller's colours count apart. Segments with one seller are counted, not measured |
| `a3_seller_positions` | From `daily_seller_positions`: where the platform's own store and the other sellers price against the segment's median seller, how often they are below it or the cheapest, and how many sellers sit more than 5% below or above it | Only segments with at least 3 sellers are compared |
| `a3_seller_map` | One row per seller with at least 10 compared segment-days: size (offers a day) and mean position, ranked by size | Ranks change from one window to the next; they name no one |
| `a6_supply_weekly` | By week: offers and sellers a day, the platform's share, new offers a day (first seen) and ended offers a day (last seen the day before) | Offers often leave stock for a day and return. New offers are counted from 3 days after the first day (one out of stock on the first day looks new when it returns), ended offers only when gone for 3 days by the end (one gone on the last days may return); `new_days` and `ended_days` say how many days each figure covers. An ended offer is not necessarily a sale (plan 10.1, A8). Stock units are left out: a few sellers' declared stock drives them (on 16.10 one seller's stock fell from 1,625 units to 17) |
| `a6_supply_by_generation`, `a6_supply_by_model` | Offers a day over the first and the last days of the period, the change and the share at the end | A few weeks of data: a change is a movement, not yet a trend |

Not here yet: A1b (one model's price over time) needs about eight weeks of data; A4 (platforms) is suspended with
one platform left; seller premiums from the price model (A3, Phase 3).
