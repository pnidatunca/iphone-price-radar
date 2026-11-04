-- A1, what more storage is worth second-hand: for each model, every step to the next storage size of the reference
-- condition, Apple's US launch price step against the step between the segments' dollar prices over the window, and
-- the share of Apple's step the refurbished price keeps. The last row is the median over the steps. Segment medians
-- mix sellers; within one seller a larger size is rarely cheaper (the larger_storage_costs_more test).

with w as ({{ analysis_window() }}),

segments as (
    select
        s.model,
        min(s.release_date) as release_date,
        s.storage_gb,
        percentile_cont(0.5) within group (order by s.median_price_usd) as price_usd,
        max(s.us_launch_price_usd) as launch_usd,
        sum(s.offer_count) as offers
    from {{ ref('daily_segment_prices') }} s
    cross join w
    where s.snapshot_date between w.window_start and w.end_day
        and s.segment_condition = '{{ var("reference_condition") }}'
        and s.us_launch_price_usd > 0
    group by s.model, s.storage_gb
    having sum(s.offer_count) >= {{ var('analysis_min_offers') }}
),

steps as (
    select
        model,
        release_date,
        storage_gb as from_gb,
        lead(storage_gb) over sizes as to_gb,
        offers as from_offers,
        lead(offers) over sizes as to_offers,
        lead(launch_usd) over sizes - launch_usd as apple_step_usd,
        lead(launch_usd) over sizes / launch_usd - 1 as apple_step,
        lead(price_usd) over sizes - price_usd as refurbished_step_usd,
        lead(price_usd) over sizes / price_usd - 1 as refurbished_step
    from segments
    window sizes as (partition by model order by storage_gb)
),

pairs as (
    select *, refurbished_step_usd / nullif(apple_step_usd, 0) as kept
    from steps
    where to_gb is not null
),

result as (
    select
        0 as summary,
        release_date,
        model,
        from_gb,
        to_gb,
        from_offers,
        to_offers,
        round(apple_step_usd::numeric) as apple_step_usd,
        round(100 * apple_step::numeric, 1) as apple_step_pct,
        round(refurbished_step_usd::numeric) as refurbished_step_usd,
        round(100 * refurbished_step::numeric, 1) as refurbished_step_pct,
        round(100 * kept::numeric) as share_of_apple_step_kept_pct
    from pairs

    union all

    select
        1,
        null,
        'median of ' || count(*) || ' steps',
        null,
        null,
        null,
        null,
        round((percentile_cont(0.5) within group (order by apple_step_usd))::numeric),
        round(100 * (percentile_cont(0.5) within group (order by apple_step))::numeric, 1),
        round((percentile_cont(0.5) within group (order by refurbished_step_usd))::numeric),
        round(100 * (percentile_cont(0.5) within group (order by refurbished_step))::numeric, 1),
        round(100 * (percentile_cont(0.5) within group (order by kept))::numeric)
    from pairs
)

select
    model,
    from_gb,
    to_gb,
    from_offers,
    to_offers,
    apple_step_usd,
    apple_step_pct,
    refurbished_step_usd,
    refurbished_step_pct,
    share_of_apple_step_kept_pct
from result
order by summary, release_date, model, from_gb
