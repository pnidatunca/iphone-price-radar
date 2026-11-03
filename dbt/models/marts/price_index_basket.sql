-- The fixed basket of the price index (plan 10.3): every segment priced on at least index_min_day_share of the base
-- period's days with at least index_min_offers offers a day on average, weighted by its share of the base period's
-- offers. Base prices are the median of the segment's daily medians. Computed from the permanent
-- daily_segment_prices of a past period, so the basket stops changing once that period is final: when its last day is
-- out of the window daily_segment_prices computes again.

{{ config(materialized='table') }}

with base as (
    select *
    from {{ ref('daily_segment_prices') }}
    where snapshot_date between date '{{ var("index_base_start") }}' and date '{{ var("index_base_end") }}'
),

base_days as (
    select platform, count(distinct snapshot_date) as days
    from base
    group by platform
),

latest as (
    select platform, max(snapshot_date) as latest_day
    from {{ ref('daily_segment_prices') }}
    group by platform
),

segments as (
    select
        platform,
        segment_id,
        min(model) as model,
        min(storage_gb) as storage_gb,
        min(segment_condition) as segment_condition,
        count(*) as days_priced,
        avg(offer_count) as avg_offers,
        sum(offer_count) as base_offers,
        percentile_cont(0.5) within group (order by median_price_tl) as base_price_tl,
        percentile_cont(0.5) within group (order by median_price_usd) as base_price_usd
    from base
    group by platform, segment_id
),

chosen as (
    select
        s.*,
        d.days as base_days,
        -- a run computes again the lookback_days days before the latest one it holds, and adds at least one day
        l.latest_day > date '{{ var("index_base_end") }}' + {{ var('lookback_days') }} + 1 as final
    from segments s
    join base_days d using (platform)
    join latest l using (platform)
    where s.days_priced >= {{ var('index_min_day_share') }} * d.days
        and s.avg_offers >= {{ var('index_min_offers') }}
)

select
    platform,
    segment_id,
    model,
    storage_gb,
    segment_condition,
    date '{{ var("index_base_start") }}' as base_start,
    date '{{ var("index_base_end") }}' as base_end,
    base_days,
    final,
    days_priced,
    round(avg_offers::numeric, 2) as avg_offers,
    base_offers,
    round(base_offers::numeric / sum(base_offers) over (partition by platform), 6) as weight,
    round(base_price_tl::numeric, 2) as base_price_tl,
    round(base_price_usd::numeric, 2) as base_price_usd
from chosen
