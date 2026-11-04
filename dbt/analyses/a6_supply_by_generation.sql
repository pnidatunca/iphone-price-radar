-- A6, how the supply of each generation changed: in-stock offers a day over the first and the last analysis_days days
-- of the period (a day without offers counts as zero), the change, and the generation's share at the end.

with w as ({{ analysis_window() }}),

day_counts as (
    select
        count(distinct m.snapshot_date) filter (where m.snapshot_date <= w.first_window_end) as first_days,
        count(distinct m.snapshot_date) filter (where m.snapshot_date >= w.window_start) as last_days
    from {{ ref('daily_market') }} m
    cross join w
    where m.snapshot_date between w.start_day and w.end_day
),

generations as (
    select
        s.generation,
        sum(s.offer_count) filter (where s.snapshot_date <= w.first_window_end) as first_offers,
        sum(s.offer_count) filter (where s.snapshot_date >= w.window_start) as last_offers
    from {{ ref('daily_segment_prices') }} s
    cross join w
    where s.snapshot_date between w.start_day and w.end_day
    group by s.generation
)

select
    g.generation,
    round(coalesce(g.first_offers, 0)::numeric / c.first_days, 1) as first_window_offers_a_day,
    round(coalesce(g.last_offers, 0)::numeric / c.last_days, 1) as last_window_offers_a_day,
    round(100 * (coalesce(g.last_offers, 0)::numeric / c.last_days)
        / nullif(g.first_offers::numeric / c.first_days, 0) - 100, 1) as change_pct,
    round(100.0 * coalesce(g.last_offers, 0) / sum(g.last_offers) over (), 1) as last_window_share_pct
from generations g
cross join day_counts c
order by g.generation desc nulls last
