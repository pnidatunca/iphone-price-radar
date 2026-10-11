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
- The crawler identifies itself honestly with a User-Agent that names this repository and a contact address.
  Site owners can use that address to ask for a source to be dropped, and it will be dropped.
- No personal data is collected.
- Raw catalogue data (images, descriptions, product lists) is not republished. Only aggregated analyses and model
  outputs are shown, with links to the original product pages.

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
