# iPhone Price Radar

Daily price intelligence for iPhones sold in Turkey (iPhone X and newer), refurbished and new. The system collects
offers from refurbished-device retailers and a marketplace every night, estimates the fair price of each device with an
uncertainty interval, and flags offers priced clearly below the market, in separate deal sections for refurbished and
new phones.

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
| Trendyol | Not collected: dropped on 19.10.2026 because it blocks requests from GitHub Actions. The adapter (refurbished and new iPhones) stays in the code, disabled |
| TCMB | Official daily USD/TRY rates, used to normalise prices |

## Data collection policy

- Only public product and category pages are requested; paths disallowed by `robots.txt` are never requested.
- Each source requests only a fixed list of URL forms; sorting, filters, search, seller pages and internal APIs are
  refused in code.
- One connection per site, 2–3.5 seconds between requests, one run per day.
- The crawler identifies itself as a bot by name (`IphonePriceRadar`), never as a browser; see
  [For site owners](#for-site-owners).
- No personal data is collected.
- Prices mean the same on every platform: `price_tl` is what a buyer pays today for the offer, `list_price_tl` the
  price before a temporary discount that the page itself shows for that offer (Trendyol's sticker price before the
  automatic basket promotion, Getmobil's struck-through price), or `price_tl` when the page shows
  none, and `promo_pct` the discount between the two. An implausible earlier price is not stored and is reported;
  per-user coupons are ignored.
- Raw catalogue data (images, descriptions, product lists) is not republished. Only aggregated analyses and model
  outputs are shown, with links to the original product pages.

## For site owners

The crawler of this project identifies itself as:

```text
IphonePriceRadar/<version>
```

- It runs once a day, sends one request at a time with 2–3.5 seconds between requests, and follows `robots.txt`,
  including groups addressed to `IphonePriceRadar`. `robots.txt` is read again on every run.
- It reads public product and category pages only and stores prices, stock and product attributes (model, storage,
  colour, condition, battery range where the page shows one) with the seller's numeric id. It does not store seller
  names, store links, legal details (official names, tax and registration numbers, e-mail addresses, addresses) or
  any other personal data.
- It does not republish your catalogue: only aggregated statistics and model outputs are shown, with links back to
  your product pages.

To exclude your site, add a `User-agent: IphonePriceRadar` group with `Disallow: /` to your `robots.txt`; it is read
on every run. For anything else, please [open an issue](https://github.com/pnidatunca/iphone-price-radar/issues).
Exclusion requests are honoured from the next run on.

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
uv run python -m price_radar.ingest.run --source getmobil --out data/runs/getmobil --max-requests 40
```

The manual `access-test` workflow runs the same command on GitHub Actions with `--summary-only`; it keeps no data
and writes only counts and timings to the job summary.

The `daily-pipeline` workflow runs every morning at 04:47 Turkey time, with a fallback at 08:47 that does nothing if
that day's run is already loaded (GitHub sometimes drops scheduled runs). Each source (Getmobil) gets one full run,
loaded into Neon with `python -m price_radar.storage.snapshots load`: one row per offer and day in
`raw.offer_snapshots` (the payload only when the offer changed), rejected offers in `raw.rejected_offers` and one
health row per run in `ops.pipeline_runs`. Nothing is uploaded as an artifact.

The `fx-rates` workflow runs every morning and stores the TCMB bulletins that are not stored yet in `ref.fx_rates`
(`python -m price_radar.fx.tcmb`, then `python -m price_radar.storage.fx_rates load`). Prices of day D are converted
with the USD forex selling rate announced on the last business day before D.

Health checks (`python -m price_radar.monitoring.health`) run after every nightly run and again in the afternoon
(`health-check` workflow), because a run that never started sends no failure e-mail. Errors: no complete run today
or yesterday, offers below half of the 7-day average, listing pages that flag the crawler as a bot. Warnings: the
request budget nearly spent, more than 5% rejected offers, stale TCMB rates. An error turns the job red; with the
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` secrets set, the nightly summary and every finding also go to Telegram
(`python -m price_radar.notify.telegram chat-id` finds the chat id after you message the bot).
