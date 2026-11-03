-- Business rule (plan 10.2): within a model and condition, a larger storage size costs more. Refurbished prices break
-- it often (in W43-W44, 38 of the pairs of neighbouring sizes had the larger one cheaper over a week, up to 25% in İyi),
-- so the test looks for what a parsing error would cause: the larger size more than 15% cheaper over the last 7 days,
-- with at least 5 offers on each side, in more than 10 pairs at once.

{{ config(severity='warn', warn_if='>10') }}

with recent as (
    select
        platform,
        model,
        segment_condition,
        storage_gb,
        avg(median_price_tl) as price_tl,
        sum(offer_count) as offers
    from {{ ref('daily_segment_prices') }}
    where snapshot_date > (select max(snapshot_date) - 7 from {{ ref('daily_segment_prices') }})
    group by platform, model, segment_condition, storage_gb
),

neighbours as (
    select
        *,
        lag(storage_gb) over sizes as smaller_gb,
        lag(price_tl) over sizes as smaller_price_tl,
        lag(offers) over sizes as smaller_offers
    from recent
    window sizes as (partition by platform, model, segment_condition order by storage_gb)
)

select platform, model, segment_condition, smaller_gb, storage_gb, smaller_price_tl, price_tl
from neighbours
where smaller_gb is not null
    and price_tl < 0.85 * smaller_price_tl
    and offers >= 5
    and smaller_offers >= 5
