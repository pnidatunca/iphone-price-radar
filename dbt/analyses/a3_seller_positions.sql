-- A3, where sellers price against each other over the window (marts.daily_seller_positions): for the platform's own
-- store and for the other sellers, the mean position against the segment's median seller (weighted by the segments
-- compared), the share of compared segments where they are below the median or the cheapest, and, over the sellers
-- with at least analysis_min_compared compared segment-days, how many price more than 5% below or above the median.

with w as ({{ analysis_window() }}),

sellers as (
    select
        p.seller_key,
        bool_or(p.seller_is_platform) as is_platform,
        sum(p.offer_count) as offer_days,
        sum(p.compared_segment_count) as compared,
        sum(p.median_position_pct * p.compared_segment_count) / nullif(sum(p.compared_segment_count), 0)
            as position_pct,
        sum(p.below_median_count) as below,
        sum(p.cheapest_count) as cheapest
    from {{ ref('daily_seller_positions') }} p
    cross join w
    where p.snapshot_date between w.window_start and w.end_day
    group by p.seller_key
),

mapped as (
    select * from sellers where compared >= {{ var('analysis_min_compared') }}
)

select
    case when s.is_platform then 'platform' else 'other sellers' end as sellers,
    count(*) as active_sellers,
    sum(s.offer_days) as offer_days,
    sum(s.compared) as compared_segment_days,
    round(sum(s.position_pct * s.compared) / nullif(sum(s.compared), 0), 1) as mean_position_pct,
    round(100.0 * sum(s.below) / nullif(sum(s.compared), 0), 1) as below_median_pct,
    round(100.0 * sum(s.cheapest) / nullif(sum(s.compared), 0), 1) as cheapest_pct,
    (select count(*) from mapped m where m.is_platform = s.is_platform) as mapped_sellers,
    (select count(*) from mapped m where m.is_platform = s.is_platform and m.position_pct < -5) as more_than_5_pct_below,
    (select count(*) from mapped m where m.is_platform = s.is_platform and m.position_pct between -5 and 5)
        as within_5_pct,
    (select count(*) from mapped m where m.is_platform = s.is_platform and m.position_pct > 5) as more_than_5_pct_above
from sellers s
group by s.is_platform
order by s.is_platform desc
