-- The headline figures of a market report (plan 10.5, sections 1 and 2), one row: the period, the market's size and
-- price level over the window, the change in offers from the first window, the TCMB rate and the price index.

with w as ({{ analysis_window() }}),

days as (
    select m.*
    from {{ ref('daily_market') }} m
    cross join w
    where m.snapshot_date between w.start_day and w.end_day
),

first_window as (
    select avg(d.offer_count) as offers, avg(d.seller_count) as sellers
    from days d
    cross join w
    where d.snapshot_date <= w.first_window_end
),

last_window as (
    select
        count(*) as days,
        avg(d.offer_count) as offers,
        avg(d.seller_count) as sellers,
        sum(d.platform_offer_count)::numeric / sum(d.offer_count) as platform_share,
        sum(d.discounted_offer_count)::numeric / sum(d.offer_count) as discounted_share,
        percentile_cont(0.5) within group (order by d.median_price_tl) as median_price_tl,
        percentile_cont(0.5) within group (order by d.median_price_usd) as median_price_usd
    from days d
    cross join w
    where d.snapshot_date >= w.window_start
),

rates as (
    select
        (select usd_try from days order by snapshot_date limit 1) as first_rate,
        (select usd_try from days order by snapshot_date desc limit 1) as last_rate
),

index_days as (
    select i.snapshot_date, i.index_tl, i.index_usd
    from {{ ref('price_index') }} i
    cross join w
    where i.snapshot_date <= w.end_day
),

index_end as (
    select * from index_days order by snapshot_date desc limit 1
),

index_window_start as (
    -- the day before the window, so the change covers analysis_days days
    select i.*
    from index_days i
    cross join w
    where i.snapshot_date < w.window_start
    order by i.snapshot_date desc
    limit 1
)

select
    w.start_day,
    w.end_day,
    (select count(*) from days) as days,
    w.window_start,
    l.days as window_days,
    round(l.offers) as offers_a_day,
    round(l.sellers) as sellers_a_day,
    round(100 * l.platform_share, 1) as platform_offer_share_pct,
    round(100 * l.discounted_share, 1) as discounted_offer_share_pct,
    round(100 * (l.offers / f.offers - 1), 1) as offers_vs_first_window_pct,
    round(l.median_price_tl::numeric) as median_price_tl,
    round(l.median_price_usd::numeric) as median_price_usd,
    round(r.first_rate, 4) as usd_try_first_day,
    round(r.last_rate, 4) as usd_try_end_day,
    round(100 * (r.last_rate / r.first_rate - 1), 2) as usd_try_change_pct,
    ie.snapshot_date as index_day,
    round(ie.index_tl, 2) as index_tl,
    round(ie.index_usd, 2) as index_usd,
    round(100 * (ie.index_tl / iw.index_tl - 1), 2) as index_tl_window_change_pct,
    round(100 * (ie.index_usd / iw.index_usd - 1), 2) as index_usd_window_change_pct
from w
cross join first_window f
cross join last_window l
cross join rates r
left join index_end ie on true
left join index_window_start iw on true
