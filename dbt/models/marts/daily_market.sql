-- Day x platform: the size and price level of the whole market, the figures of the dashboards' overview (plan 10.4).
-- Permanent, with the same lookback as daily_segment_prices.

{{ config(
    unique_key=['snapshot_date', 'platform'],
    incremental_strategy='delete+insert',
    on_schema_change='fail',
) }}

select
    snapshot_date,
    platform,
    count(*) as offer_count,
    count(distinct seller_key) as seller_count,
    count(distinct model) as model_count,
    count(distinct device_id) as device_count,
    count(distinct segment_id) as segment_count,
    count(*) filter (where seller_is_platform) as platform_offer_count,
    sum(stock_qty) as stock_units,
    round(percentile_cont(0.5) within group (order by price_tl)::numeric, 2) as median_price_tl,
    round(percentile_cont(0.5) within group (order by price_usd)::numeric, 2) as median_price_usd,
    count(*) filter (where promo_pct > 0) as discounted_offer_count,
    min(usd_try) as usd_try,
    bool_and(fx_confirmed) as fx_confirmed,
    now() as computed_at
from {{ ref('fct_offer_daily') }}
where in_stock
{% if is_incremental() %}
    and snapshot_date >= {{ lookback_start('snapshot_date') }}
{% endif %}
group by snapshot_date, platform
