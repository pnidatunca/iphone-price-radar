-- A6, the supply week by week (Monday to Sunday) over the whole period: in-stock offers and sellers a day, the
-- platform's own share, and the flow of offers: new offers a day (first seen that day; the first day of the data is
-- left out, it has nothing to compare with) and offers that ended a day (last seen the day before; one listed again
-- later counts as ended only after its last day). Stock units are left out: a few sellers' declared stock drives them.

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
        (select count(*) from {{ ref('offer_lifecycle') }} o where o.first_seen = d.snapshot_date
            and d.snapshot_date > w.start_day) as new_offers,
        (select count(*) from {{ ref('offer_lifecycle') }} o where o.last_seen = d.snapshot_date - 1
            and d.snapshot_date > w.start_day) as ended_offers
    from days d
    cross join w
)

select
    date_trunc('week', d.snapshot_date)::date as week_start,
    count(*) as days,
    round(avg(d.offer_count)) as offers_a_day,
    round(avg(d.seller_count)) as sellers_a_day,
    round(100.0 * sum(d.platform_offer_count) / sum(d.offer_count), 1) as platform_offer_share_pct,
    round(avg(f.new_offers) filter (where d.snapshot_date > w.start_day), 1) as new_offers_a_day,
    round(avg(f.ended_offers) filter (where d.snapshot_date > w.start_day), 1) as ended_offers_a_day
from days d
join flows f using (snapshot_date)
cross join w
group by date_trunc('week', d.snapshot_date)
order by week_start
