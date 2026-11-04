-- A1a, the depreciation curve as one number: a least-squares fit of ln(price / US launch price) on age in years over
-- the same segments as a1_age_curve, for all of them and for the Pro models and the others apart. exp(slope) - 1 is
-- the change in the dollar price per year of age; the ratios at 1, 3 and 5 years are read off the fitted line.

with w as ({{ analysis_window() }}),

segments as (
    select
        case when s.series in ('Pro', 'Pro Max') then 'Pro and Pro Max' else 'other models' end as series_group,
        avg(s.device_age_days) / 365.25 as age_years,
        percentile_cont(0.5) within group (order by s.median_price_usd) / max(s.us_launch_price_usd) as ratio
    from {{ ref('daily_segment_prices') }} s
    cross join w
    where s.snapshot_date between w.window_start and w.end_day
        and s.segment_condition = '{{ var("reference_condition") }}'
        and s.us_launch_price_usd > 0
    group by s.series, s.device_id
    having sum(s.offer_count) >= {{ var('analysis_min_offers') }}
),

fits as (
    select
        coalesce(series_group, 'all models') as models,
        grouping(series_group) as total,
        count(*) as segments,
        min(age_years) as youngest,
        max(age_years) as oldest,
        regr_slope(ln(ratio), age_years) as slope,
        regr_intercept(ln(ratio), age_years) as intercept,
        regr_r2(ln(ratio), age_years) as r2
    from segments
    group by grouping sets ((), (series_group))
)

select
    models,
    segments,
    round(youngest::numeric, 1) as youngest_years,
    round(oldest::numeric, 1) as oldest_years,
    round(100 * (exp(slope) - 1)::numeric, 1) as change_per_year_pct,
    round(r2::numeric, 3) as r2,
    round(exp(intercept + slope)::numeric, 2) as ratio_at_1_year,
    round(exp(intercept + 3 * slope)::numeric, 2) as ratio_at_3_years,
    round(exp(intercept + 5 * slope)::numeric, 2) as ratio_at_5_years
from fits
order by total desc, models
