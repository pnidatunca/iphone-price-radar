-- Day x platform x seller: where a seller prices against the other sellers of the same segment (A3, the anonymous
-- seller map of plan 10.4). A seller's price in a segment is the median over its offers there (colours, SIM types);
-- its position is that price over the median of the sellers' prices in the segment, minus 1. Only segments with at
-- least position_min_sellers sellers are compared. Seller numeric ids only (K22). Permanent, with the usual lookback.

{{ config(
    unique_key=['snapshot_date', 'platform', 'seller_key'],
    incremental_strategy='delete+insert',
    on_schema_change='fail',
) }}

with seller_prices as (
    select
        snapshot_date,
        platform,
        segment_id,
        seller_key,
        bool_or(seller_is_platform) as seller_is_platform,
        count(*) as offers,
        percentile_cont(0.5) within group (order by price_tl) as price_tl
    from {{ ref('fct_offer_daily') }}
    where in_stock
    {% if is_incremental() %}
        and snapshot_date >= {{ lookback_start('snapshot_date') }}
    {% endif %}
    group by snapshot_date, platform, segment_id, seller_key
),

segments as (
    select
        snapshot_date,
        platform,
        segment_id,
        count(*) as sellers,
        percentile_cont(0.5) within group (order by price_tl) as median_price_tl,
        min(price_tl) as min_price_tl
    from seller_prices
    group by snapshot_date, platform, segment_id
),

positions as (
    select
        p.*,
        s.sellers >= {{ var('position_min_sellers') }} as compared,
        p.price_tl / s.median_price_tl - 1 as position,
        p.price_tl = s.min_price_tl as cheapest
    from seller_prices p
    join segments s using (snapshot_date, platform, segment_id)
)

select
    snapshot_date,
    platform,
    seller_key,
    bool_or(seller_is_platform) as seller_is_platform,
    sum(offers) as offer_count,
    count(*) as segment_count,
    count(*) filter (where compared) as compared_segment_count,
    round(100 * (percentile_cont(0.5) within group (order by position) filter (where compared))::numeric, 2)
        as median_position_pct,
    round(100 * (percentile_cont(0.25) within group (order by position) filter (where compared))::numeric, 2)
        as p25_position_pct,
    round(100 * (percentile_cont(0.75) within group (order by position) filter (where compared))::numeric, 2)
        as p75_position_pct,
    count(*) filter (where compared and position < 0) as below_median_count,
    count(*) filter (where compared and position > 0) as above_median_count,
    count(*) filter (where compared and cheapest) as cheapest_count,
    now() as computed_at
from positions
group by snapshot_date, platform, seller_key
