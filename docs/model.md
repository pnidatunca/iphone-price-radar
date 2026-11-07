# Price model

The price model of Phase 3 (plan 11) estimates what an offer should cost from what it is: model, storage, condition,
age. The production model will flag offers priced clearly below that (deals). This page describes the training set,
the models built so far and how they are tested, and gives the latest results.

Status (7.11.2026): training set, M0, M1 and the rolling backtest. Next: M2 and M2-ns (LightGBM with monotonic
constraints), conformal intervals, then the deal rule.

## Training set

`python -m price_radar.model.dataset extract` reads one row per in-stock offer and day from `core.fct_offer_daily`
in a read-only transaction and writes it to a local Parquet file (default `data/model/offer_daily.parquet`). The
file holds numeric seller ids, which M2 needs for seller premiums, so it stays on the machine or the runner that made
it. `data/` and `*.parquet` are never committed (K12), and no workflow uploads the file.

- **Offer-price pairs (plan 11.3).** One offer at one price is one training row, however long it stays on sale.
  Otherwise an offer on sale for 60 days at the same price would weigh 60 times. A pair's features are those of the
  day it was first seen, and its dollar price uses that day's TCMB rate (plan 5.4). On 12.10-7.11: 7,405 pairs from
  42,872 offer-days.
- **Target (plan 11.2).** The log of the dollar price.
- **Outliers (plan 9).** An offer-day is an outlier when its dollar price is below 0.1 or above 5 times the US
  launch price, or, in a segment with at least 3 offers that day, below half or above twice the segment's median. A
  pair is an outlier when its first day is. Outliers are left out of training and testing. On 12.10-7.11: 1 pair.
- **Thin segments (K30).** A pair is in a thin segment when the segment had fewer than 5 offers in the 7 days up to
  its first day. Errors are reported for thin and dense segments apart.

## Models

| Code | Model | Status |
|---|---|---|
| M0 | Baseline: the median log price of the segment's pairs first seen in the 30 days before the test week. A segment without any falls back to its earlier pairs, then to the device (model and storage, any condition), the model, all pairs | Done |
| M1 | Log-linear hedonic model, least squares: age, log US launch price, log2 storage, Pro, condition (against Mükemmel), eSIM | Done |
| M2 | LightGBM with monotonic constraints, seller included; for seller premiums only | Next |
| M2-ns | M2 without the seller: the production model for valuations and deals (K16) | Next |

M1 is for coefficients one can read, not for prediction. Dummies for every series predict a little better (MAPE 9.2%
against 9.7% on 1-7.11) but carry the same information as the launch price, which turns the launch price coefficient
negative and meaningless. The Pro flag keeps it interpretable.

## Backtest

`python -m price_radar.model.backtest` tests every model week by week (plan 11.6):

- Each fold's test week is one of the latest 7-day periods. Its training set is the pairs first seen in the 83 days
  before it (the 90-day window, plan 11.3), with at least 10 days of them.
- The test week is scored on the pairs **first seen in it** (`new`), which no model has seen (K15). That is the
  measure that counts. It is scored again on **every pair on sale** in it (`on sale`), which includes pairs the model
  trained on. A large gap between the two means the model memorises offers.
- Metrics: MAPE, the median absolute percentage error (MdAPE) and the mean absolute error in lira at each pair's
  rate. They are reported by fold, pooled over the folds, and for new pairs by series, condition and segment density.

The output (`data/model/backtest`: CSV files and `backtest.md`) holds aggregates only. A table with a column that
identifies a seller or an offer is refused (K22).

On Neon, run the **backtest** workflow by hand (Actions → backtest → Run workflow). It extracts the offers into the
runner's temporary directory and puts the results in the job summary and a 14-day artifact. Locally:

```bash
uv sync --group model
uv run python -m price_radar.model.dataset extract   # needs NEON_DATABASE_URL
uv run python -m price_radar.model.backtest
```

## Results

Data 12.10-7.11. 2 folds: test weeks 25-31.10 (4,488 training pairs) and 1-7.11 (5,912).

| Model | Pairs | MAPE, new | MdAPE, new | MAE, new | MAPE, on sale |
|---|---|---|---|---|---|
| M0 | 2,916 | 9.3% | 7.3% | 5,891 TL | 9.3% |
| M1 | 2,916 | 10.0% | 7.7% | 6,168 TL | 10.6% |

- **M0 is hard to beat.** In the test weeks 99% of new pairs belonged to a segment seen in the 30 days before.
  M2-ns has to beat it clearly on new pairs (plan 11.6).
- **No sign of memorisation.** M0 and M1 err about as much on new pairs as on all pairs on sale.
- **Most of the error is the spread between sellers.** M0 errs least where one seller dominates (Premium+ 6.3%,
  Plus 6.0%) and most where many sellers offer the same phone (Mükemmel 11.3%). Within a segment the interquartile
  range of the day's offers is 7-18% (pilot report, A3). A model without the seller (M2-ns, K16) cannot explain that
  spread, and the cheap end of it is what deal detection looks for.
- **Thin segments** are harder: M0's MAPE is 11.2% there against 9.0% in dense ones (K30).
- **Small series are poor:** SE (32 pairs, M0 15.5%) and the e models (6 pairs). M2 will be checked there first.

M1's coefficients on the latest fold (5,912 pairs):

| Term | Effect |
|---|---|
| One more year of age | −23.0% |
| US launch price | elasticity 0.85: a launch price 10% higher, a price about 8.5% higher |
| Pro and Pro Max | +10.5% beyond their launch price |
| Double the storage at the same launch price | −2.8%: the market pays less for storage than Apple's price step |
| İyi, against Mükemmel | −8.1% |
| Premium | −2.8% |
| Premium+ | +0.1% |
| eSIM-only | −7.1% (few offers) |

The condition terms compare offers across sellers, like segment medians do: Getmobil's own store sells most Premium
offers below other sellers. The figures within the same seller are in the
[pilot report](../reports/2026-11-pilot.md) (Premium −0.1%, Premium+ +3.8%, İyi −6.1%). The age term is close to
the report's −22.1% a year, which was fitted on Mükemmel segments.
