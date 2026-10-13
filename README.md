# iPhone Price Radar

Daily price intelligence for refurbished iPhones sold in Turkey. The system collects offers from refurbished-device
retailers every night, estimates the fair price of each device with an uncertainty interval, and flags offers priced
clearly below the market.

**Status:** Phase 0 (feasibility), October 2026.

## What it will do

| Layer | Output |
|---|---|
| Data pipeline | Nightly collection → validation → PostgreSQL, fully automated with GitHub Actions |
| Analytics | dbt models, a Power BI dashboard, a public market page, a refurbished-iPhone price index, monthly market reports |
| Modelling | A hedonic price model (LightGBM with monotonic constraints) with conformal prediction intervals; deal detection |
| MLOps | Weekly retraining with a champion/challenger gate, MLflow tracking, drift monitoring, a Streamlit app |

## Data sources

| Source | Role |
|---|---|
| Getmobil | Refurbished-device marketplace (multiple sellers) |
| Easycep | Refurbished-device retailer (unit-level listings) |
| TCMB | Official daily USD/TRY rates, used to normalise prices |

## Data collection policy

- Only public product and category pages are requested; paths disallowed by `robots.txt` are never requested.
- One connection per site, 2–3.5 seconds between requests, one run per day.
- The crawler identifies itself honestly; its User-Agent links to the [notice for site owners](#for-site-owners).
- No personal data is collected.
- Raw catalogue data (images, descriptions, product lists) is not republished. Only aggregated analyses and model
  outputs are shown, with links to the original product pages.

## For site owners

The crawler of this project identifies itself as:

```text
IphonePriceRadar/<version> (+https://github.com/pnidatunca/iphone-price-radar#for-site-owners)
```

- It runs once a day, sends one request at a time with 2–3.5 seconds between requests, and follows `robots.txt`,
  including groups addressed to `IphonePriceRadar`. `robots.txt` is read again on every run.
- It reads public product and category pages only and stores prices, stock and product attributes (model, storage,
  colour, condition, battery range where the page shows one) with the seller's numeric id. It does not store seller
  names, store links or any other personal data.
- It does not republish your catalogue: only aggregated statistics and model outputs are shown, with links back to
  your product pages.

To ask for changes or for your site to be excluded, please
[open an issue](https://github.com/pnidatunca/iphone-price-radar/issues). Exclusion requests are honoured from the
next run on.

## Roadmap

| Phase | Focus |
|---|---|
| 0 | Feasibility: source structure, request budget, storage estimate, start of raw data collection |
| 1 | Data pipeline: adapters, validation, PostgreSQL, nightly GitHub Actions run, alerts |
| 2 | Analytics: dbt models, Power BI dashboard, public market page, first market report |
| 3 | Modelling: price model, prediction intervals, deal detection, experiment tracking |
| 4 | MLOps and app: weekly retraining, drift monitoring, full Streamlit app |

## Development

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync               # create the environment (Python 3.12)
uv run pytest         # run the tests
uv run ruff check .   # lint
```

Configuration lives in `config/settings.yaml`. Secrets (database URL, tokens) are read from environment variables;
copy `.env.example` to `.env` for local runs.

One collection run of one source writes `offers.jsonl` and `run_summary.json` (`data/` is not committed):

```bash
uv run python -m price_radar.ingest.run --source easycep --out data/runs/easycep --max-requests 40
```

The manual `access-test` workflow runs the same command on GitHub Actions with `--summary-only`; it keeps no data
and writes only counts and timings to the job summary.

The `trial-crawl` workflow (Phase 0) runs every morning at 04:47 Turkey time, with a fallback at 08:47 that does
nothing if that day's run is already loaded (GitHub sometimes drops scheduled runs). Each source gets one full run,
loaded into the `raw.landing` table in Neon with `python -m price_radar.storage.landing load`. Nothing is uploaded as
an artifact.
