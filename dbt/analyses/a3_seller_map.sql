-- A3, the anonymous seller map (plan 10.4): one row per seller with at least analysis_min_compared compared
-- segment-days over the window, its size, its mean position against the segment's median seller and the band of
-- a3_seller_positions it falls in (more than 5% below the median, within 5%, more than 5% above). Sellers carry a
-- rank by offers instead of their id (K22): the rank changes from one window to the next and names no one.

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
)

select
    row_number() over (order by offer_days desc, position_pct, seller_key) as seller_rank,
    is_platform,
    round(offer_days::numeric / (select count(distinct snapshot_date) from {{ ref('daily_market') }}, w
        where snapshot_date between w.window_start and w.end_day), 1) as offers_a_day,
    compared as compared_segment_days,
    round(position_pct, 1) as mean_position_pct,
    -- the bands of a3_seller_positions, on the unrounded position
    case when position_pct < -5 then 'below' when position_pct > 5 then 'above' else 'within' end as position_band,
    round(100.0 * below / compared, 1) as below_median_pct,
    round(100.0 * cheapest / compared, 1) as cheapest_pct
from sellers
where compared >= {{ var('analysis_min_compared') }}
order by seller_rank
