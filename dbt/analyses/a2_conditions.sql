-- A2, what a condition costs against the reference condition (Mükemmel), measured two ways over the window:
-- segment_median_pct compares the segments' median prices for the same device and day (plan 10.1's first method);
-- within_seller_*_pct compares a seller's price with its own reference-condition offer of the same device and day
-- (marts.daily_condition_premiums, the mean of the daily medians weighted by the pairs compared), for all sellers,
-- the platform's own store and the others. The two disagree when the platform's share differs between conditions:
-- the segment medians then compare sellers as much as conditions.

with w as ({{ analysis_window() }}),

segments as (
    select s.*
    from {{ ref('daily_segment_prices') }} s
    cross join w
    where s.snapshot_date between w.window_start and w.end_day
),

supply as (
    select
        segment_condition as condition,
        max(condition_level) as condition_level,
        sum(offer_count) as offers,
        sum(platform_offer_count) as platform_offers
    from segments
    group by segment_condition
),

segment_medians as (
    select
        s.segment_condition as condition,
        count(*) as device_days,
        percentile_cont(0.5) within group (order by s.median_price_tl / r.median_price_tl - 1) as difference
    from segments s
    join segments r
        on r.snapshot_date = s.snapshot_date
        and r.platform = s.platform
        and r.device_id = s.device_id
        and r.segment_condition = '{{ var("reference_condition") }}'
    where s.segment_condition <> '{{ var("reference_condition") }}'
    group by s.segment_condition
),

within_seller as (
    select
        p.condition_raw as condition,
        p.seller_group,
        sum(p.pairs) as pairs,
        sum(p.median_difference_pct * p.pairs) / sum(p.pairs) as difference_pct
    from {{ ref('daily_condition_premiums') }} p
    cross join w
    where p.snapshot_date between w.window_start and w.end_day
    group by p.condition_raw, p.seller_group
)

select
    s.condition,
    s.condition_level,
    s.offers,
    round(100.0 * s.offers / sum(s.offers) over (), 1) as offer_share_pct,
    round(100.0 * s.platform_offers / s.offers, 1) as platform_share_pct,
    m.device_days,
    round(100 * m.difference::numeric, 1) as segment_median_pct,
    max(ws.pairs) filter (where ws.seller_group = 'all') as within_seller_pairs,
    round(max(ws.difference_pct) filter (where ws.seller_group = 'all'), 1) as within_seller_pct,
    round(max(ws.difference_pct) filter (where ws.seller_group = 'platform'), 1) as within_seller_platform_pct,
    round(max(ws.difference_pct) filter (where ws.seller_group = 'others'), 1) as within_seller_others_pct
from supply s
left join segment_medians m on m.condition = s.condition
left join within_seller ws on ws.condition = s.condition
group by s.condition, s.condition_level, s.offers, s.platform_offers, m.device_days, m.difference
order by s.condition_level desc, s.offers desc
