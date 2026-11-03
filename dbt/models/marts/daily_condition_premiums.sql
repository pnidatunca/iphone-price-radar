-- Day x platform x condition: how a seller prices a condition against its own offer of the reference condition for
-- the same model and storage on the same day (A2). Comparing within a seller keeps the seller mix out: on Getmobil the
-- platform itself sells most Premium offers, so a comparison of segment medians mixes condition and seller.
-- Seller groups: all, platform (the platform's own offers) and others. Permanent, with the usual lookback.

{{ config(
    unique_key=['snapshot_date', 'platform', 'condition_raw', 'seller_group'],
    incremental_strategy='delete+insert',
    on_schema_change='fail',
) }}

with seller_prices as (
    -- one price per seller, device and condition: the median over its colours and SIM types
    select
        snapshot_date,
        platform,
        seller_key,
        seller_is_platform,
        device_id,
        condition_raw,
        percentile_cont(0.5) within group (order by price_tl) as price_tl
    from {{ ref('fct_offer_daily') }}
    where in_stock
    {% if is_incremental() %}
        and snapshot_date >= {{ lookback_start('snapshot_date') }}
    {% endif %}
    group by snapshot_date, platform, seller_key, seller_is_platform, device_id, condition_raw
),

pairs as (
    select
        s.snapshot_date,
        s.platform,
        s.condition_raw,
        s.seller_key,
        s.device_id,
        s.seller_is_platform,
        s.price_tl / r.price_tl - 1 as difference
    from seller_prices s
    join seller_prices r
        on r.snapshot_date = s.snapshot_date
        and r.seller_key = s.seller_key
        and r.device_id = s.device_id
        and r.condition_raw = '{{ var("reference_condition") }}'
    where s.condition_raw <> '{{ var("reference_condition") }}'
),

grouped as (
    select *, 'all' as seller_group from pairs
    union all
    select *, case when seller_is_platform then 'platform' else 'others' end as seller_group from pairs
)

select
    snapshot_date,
    platform,
    condition_raw,
    seller_group,
    '{{ var("reference_condition") }}' as reference_condition,
    count(*) as pairs,
    count(distinct seller_key) as sellers,
    count(distinct device_id) as devices,
    round(100 * percentile_cont(0.5) within group (order by difference)::numeric, 2) as median_difference_pct,
    round(100 * percentile_cont(0.25) within group (order by difference)::numeric, 2) as p25_difference_pct,
    round(100 * percentile_cont(0.75) within group (order by difference)::numeric, 2) as p75_difference_pct,
    now() as computed_at
from grouped
group by snapshot_date, platform, condition_raw, seller_group
