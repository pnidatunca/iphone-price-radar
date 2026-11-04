-- A3, how far prices of the same segment (model, storage, condition) lie apart on one day, over the window, by the
-- number of sellers in the segment: the interquartile range and the full range of its offers over their median, how
-- far the cheapest offer is below the median, and what that saves in lira (medians over the segment-days). Segments
-- with one seller are counted but not measured: their spread is one seller's colours.

with w as ({{ analysis_window() }}),

segments as (
    select
        s.*,
        case
            when s.seller_count = 1 then '1'
            when s.seller_count <= 3 then '2-3'
            when s.seller_count <= 9 then '4-9'
            else '10+'
        end as sellers
    from {{ ref('daily_segment_prices') }} s
    cross join w
    where s.snapshot_date between w.window_start and w.end_day
),

measured as (
    select
        coalesce(sellers, 'all') as sellers,
        grouping(sellers) as total,
        min(seller_count) as fewest,
        count(*) as segment_days,
        sum(offer_count) as offers,
        percentile_cont(0.5) within group (order by (p75_price_tl - p25_price_tl) / median_price_tl)
            filter (where seller_count > 1) as iqr,
        percentile_cont(0.5) within group (order by (max_price_tl - min_price_tl) / median_price_tl)
            filter (where seller_count > 1) as price_range,
        percentile_cont(0.5) within group (order by 1 - min_price_tl / median_price_tl)
            filter (where seller_count > 1) as cheapest_below,
        percentile_cont(0.5) within group (order by median_price_tl - min_price_tl)
            filter (where seller_count > 1) as saving_tl
    from segments
    group by grouping sets ((sellers), ())
)

select
    sellers,
    segment_days,
    offers,
    round(100.0 * segment_days / max(segment_days) over (), 1) as segment_day_share_pct,
    round(100 * iqr::numeric, 1) as iqr_pct,
    round(100 * price_range::numeric, 1) as range_pct,
    round(100 * cheapest_below::numeric, 1) as cheapest_below_median_pct,
    round(saving_tl::numeric) as cheapest_saving_tl
from measured
order by total, fewest
