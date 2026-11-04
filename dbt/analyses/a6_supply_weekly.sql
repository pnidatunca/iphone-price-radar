-- A6, the supply week by week (Monday to Sunday) over the whole period: in-stock offers and sellers a day, the
-- platform's own share, and the flow of offers. New offers a day are first seen that day; ended offers a day were last
-- seen the day before. Offers often leave stock for a day and return, so both flows are counted only where that cannot
-- mislead: new offers from analysis_settle_days days after the first day (an offer out of stock on the first day
-- would look new when it returns), ended offers only when the offer has been gone for analysis_settle_days days by
-- the end (one gone on the last days may still return). Stock units are left out: a few sellers' declared stock
-- drives them.

with w as ({{ analysis_window() }}),

days as (
    select m.snapshot_date, m.offer_count, m.seller_count, m.platform_offer_count
    from {{ ref('daily_market') }} m
    cross join w
    where m.snapshot_date between w.start_day and w.end_day
),

flows as (
    select
        d.snapshot_date,
        d.snapshot_date >= w.start_day + {{ var('analysis_settle_days') }} as new_counted,
        d.snapshot_date > w.start_day
            and d.snapshot_date - 1 <= w.end_day - {{ var('analysis_settle_days') }} as ended_counted,
        (select count(*) from {{ ref('offer_lifecycle') }} o where o.first_seen = d.snapshot_date) as new_offers,
        (select count(*) from {{ ref('offer_lifecycle') }} o where o.last_seen = d.snapshot_date - 1) as ended_offers
    from days d
    cross join w
)

select
    date_trunc('week', d.snapshot_date)::date as week_start,
    count(*) as days,
    round(avg(d.offer_count)) as offers_a_day,
    round(avg(d.seller_count)) as sellers_a_day,
    round(100.0 * sum(d.platform_offer_count) / sum(d.offer_count), 1) as platform_offer_share_pct,
    count(*) filter (where f.new_counted) as new_days,
    round(avg(f.new_offers) filter (where f.new_counted), 1) as new_offers_a_day,
    count(*) filter (where f.ended_counted) as ended_days,
    round(avg(f.ended_offers) filter (where f.ended_counted), 1) as ended_offers_a_day
from days d
join flows f using (snapshot_date)
group by date_trunc('week', d.snapshot_date)
order by week_start
