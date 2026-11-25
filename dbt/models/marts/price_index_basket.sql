-- The baskets of the price index (plan 10.3). The first is the base period's (index_base_start to index_base_end).
-- From index_renewal_start a new basket takes over on the first day of every month, chosen from the month before it,
-- so that models launched later enter the index. A basket holds every segment priced on at least index_min_day_share
-- of its reference period's days with at least index_min_offers offers a day on average, weighted by its share of the
-- period's offers. Its reference prices are the medians of the segment's daily medians over the period.
--
-- Computed from the permanent daily_segment_prices. A basket stops changing once its reference period is final: when
-- the period's last day is out of the window daily_segment_prices computes again.

{{ config(materialized='table') }}

{% if var('index_renewal_start') <= var('index_base_end') %}
    {{ exceptions.raise_compiler_error("var index_renewal_start must come after index_base_end") }}
{% endif %}

with latest as (
    select platform, max(snapshot_date) as latest_day
    from {{ ref('daily_segment_prices') }}
    group by platform
),

starts as (
    -- the base period's basket, then one a month from index_renewal_start up to the latest day
    select
        platform,
        date '{{ var("index_base_start") }}' as basket_start,
        date '{{ var("index_base_start") }}' as ref_start,
        date '{{ var("index_base_end") }}' as ref_end
    from latest
    union all
    select
        l.platform,
        m.month_start::date,
        (m.month_start - interval '1 month')::date,
        m.month_start::date - 1
    from latest l
    cross join lateral generate_series(
        date '{{ var("index_renewal_start") }}'::timestamp, l.latest_day::timestamp, interval '1 month'
    ) as m (month_start)
),

periods as (
    select
        *,
        -- the day before the next basket starts; empty for the latest basket
        lead(basket_start) over (partition by platform order by basket_start) - 1 as basket_end
    from starts
),

priced as (
    select
        r.platform,
        r.basket_start,
        p.snapshot_date,
        p.segment_id,
        p.model,
        p.storage_gb,
        p.segment_condition,
        p.offer_count,
        p.median_price_tl,
        p.median_price_usd
    from periods r
    join {{ ref('daily_segment_prices') }} p
        on p.platform = r.platform and p.snapshot_date between r.ref_start and r.ref_end
),

period_days as (
    select platform, basket_start, count(distinct snapshot_date) as ref_days
    from priced
    group by platform, basket_start
),

segments as (
    select
        platform,
        basket_start,
        segment_id,
        min(model) as model,
        min(storage_gb) as storage_gb,
        min(segment_condition) as segment_condition,
        count(*) as days_priced,
        avg(offer_count) as avg_offers,
        sum(offer_count) as ref_offers,
        percentile_cont(0.5) within group (order by median_price_tl) as ref_price_tl,
        percentile_cont(0.5) within group (order by median_price_usd) as ref_price_usd
    from priced
    group by platform, basket_start, segment_id
),

chosen as (
    select
        s.*,
        r.basket_end,
        r.ref_start,
        r.ref_end,
        d.ref_days,
        -- a run computes again the lookback_days days before the latest one it holds, and adds at least one day
        l.latest_day > r.ref_end + {{ var('lookback_days') }} + 1 as final
    from segments s
    join periods r using (platform, basket_start)
    join period_days d using (platform, basket_start)
    join latest l using (platform)
    where s.days_priced >= {{ var('index_min_day_share') }} * d.ref_days
        and s.avg_offers >= {{ var('index_min_offers') }}
)

select
    platform,
    basket_start,
    basket_end,
    segment_id,
    model,
    storage_gb,
    segment_condition,
    ref_start,
    ref_end,
    ref_days,
    final,
    days_priced,
    round(avg_offers::numeric, 2) as avg_offers,
    ref_offers,
    round(ref_offers::numeric / sum(ref_offers) over (partition by platform, basket_start), 6) as weight,
    round(ref_price_tl::numeric, 2) as ref_price_tl,
    round(ref_price_usd::numeric, 2) as ref_price_usd
from chosen
