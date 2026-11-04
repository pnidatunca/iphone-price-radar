-- A1a, cross-sectional depreciation by generation (plan 10.1). Each segment of the reference condition with at least
-- analysis_min_offers offers over the window: its dollar price (the median of its daily medians) over its US launch
-- price. Per generation, the offer-weighted mean of that ratio, and the change from the next newer generation: what
-- one more year of age costs. US launch prices exclude Turkish taxes, so a ratio above 1 is normal; the curve
-- compares models of different ages on the same days, not one model as it ages (A1b, from week 8).

with w as ({{ analysis_window() }}),

segments as (
    select
        s.generation,
        s.model,
        s.device_id,
        avg(s.device_age_days) / 365.25 as age_years,
        percentile_cont(0.5) within group (order by s.median_price_usd) as price_usd,
        max(s.us_launch_price_usd) as launch_usd,
        sum(s.offer_count) as offers
    from {{ ref('daily_segment_prices') }} s
    cross join w
    where s.snapshot_date between w.window_start and w.end_day
        and s.segment_condition = '{{ var("reference_condition") }}'
        and s.us_launch_price_usd > 0
    group by s.generation, s.model, s.device_id
    having sum(s.offer_count) >= {{ var('analysis_min_offers') }}
),

generations as (
    select
        generation,
        count(distinct model) as models,
        count(*) as segments,
        sum(offers) as offers,
        sum(age_years * offers) / sum(offers) as age_years,
        sum(price_usd / launch_usd * offers) / sum(offers) as price_to_us_launch,
        percentile_cont(0.5) within group (order by price_usd) as median_price_usd
    from segments
    group by generation
)

select
    generation,
    models,
    segments,
    offers,
    round(age_years::numeric, 1) as age_years,
    round(median_price_usd::numeric) as median_price_usd,
    round(price_to_us_launch::numeric, 3) as price_to_us_launch,
    round(100 * (price_to_us_launch / lead(price_to_us_launch) over (order by generation) - 1)::numeric, 1)
        as vs_next_newer_generation_pct
from generations
order by generation desc
