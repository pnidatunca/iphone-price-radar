-- Day x platform x segment: offer count and price distribution in lira and dollars (plan 8.2, 8.3). Permanent. The
-- last lookback_days days are computed again on every run, so offers a later run adds are not lost, and unique_key
-- keeps the rows single.

{{ config(
    unique_key=['snapshot_date', 'platform', 'segment_id'],
    incremental_strategy='delete+insert',
    on_schema_change='fail',
) }}

select
    snapshot_date,
    platform,
    segment_id,
    segment_basis,
    device_id,
    model,
    storage_gb,
    segment_condition,
    condition_level,
    generation,
    series,
    release_date,
    snapshot_date - release_date as device_age_days,
    us_launch_price_usd,
    count(*) as offer_count,
    count(distinct seller_key) as seller_count,
    count(*) filter (where seller_is_platform) as platform_offer_count,
    sum(stock_qty) as stock_units,
    round(percentile_cont(0.5) within group (order by price_tl)::numeric, 2) as median_price_tl,
    round(percentile_cont(0.25) within group (order by price_tl)::numeric, 2) as p25_price_tl,
    round(percentile_cont(0.75) within group (order by price_tl)::numeric, 2) as p75_price_tl,
    min(price_tl) as min_price_tl,
    max(price_tl) as max_price_tl,
    round(percentile_cont(0.5) within group (order by price_usd)::numeric, 2) as median_price_usd,
    min(price_usd) as min_price_usd,
    max(price_usd) as max_price_usd,
    min(usd_try) as usd_try,
    bool_and(fx_confirmed) as fx_confirmed,
    now() as computed_at
from {{ ref('fct_offer_daily') }}
where in_stock
{% if is_incremental() %}
    and snapshot_date >= {{ lookback_start('snapshot_date') }}
{% endif %}
group by
    snapshot_date,
    platform,
    segment_id,
    segment_basis,
    device_id,
    model,
    storage_gb,
    segment_condition,
    condition_level,
    generation,
    series,
    release_date,
    us_launch_price_usd
