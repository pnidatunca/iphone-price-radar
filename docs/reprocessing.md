# Rebuilding the permanent marts

The marts (`marts.daily_segment_prices`, `daily_market`, `offer_lifecycle`, `price_index`) are incremental dbt models
with `full_refresh: false`. The raw detail they summarise is deleted after 90 days (plan 8.3), so `dbt build
--full-refresh` must never rebuild them from what is left: dbt ignores the flag for these models. A rebuild is a
deliberate step and drops the table first.

## When a rebuild is needed

| Change | What to rebuild | Signal |
|---|---|---|
| Segment basis (`vars.segment_condition` in `dbt/dbt_project.yml`) | `core.fct_offer_daily` (a full refresh is fine), `daily_segment_prices`, `offer_lifecycle`, `price_index_basket`, `price_index` | test `segment_basis_unchanged` fails |
| Index base period or basket rules (`vars.index_*`) | nothing: `price_index` writes all its days again when the basket changes | test `price_index_one_basket` fails only if days from an older basket remain (a later base start) |
| A bug in a mart's SQL | that mart | — |
| A parser fix that corrects raw rows | the marts that read the changed columns | — |

## While raw holds every day (until about 10.01.2027)

Collection started on 12.10.2026 and the first rows are deleted 90 days later, so until the retention step deletes
anything the raw tables still hold the whole history and a rebuild is a drop and a build:

```sql
drop table marts.daily_segment_prices;  -- and every other table of the list above
```

Then run the `transform` workflow (Actions, Run workflow) with `select` empty, or locally from a network that reaches
Neon:

```bash
uv run python -m price_radar.transform.runner build
```

## After the retention step

Rows older than 90 days exist only in the weekly Parquet archive (`ops.archive_runs` lists every file). A rebuild then
loads the archived weeks back into a scratch database (a local PostgreSQL or a Neon branch), builds the marts there and
copies them over. The procedure will be written and tested with the retention step, before it deletes anything.
