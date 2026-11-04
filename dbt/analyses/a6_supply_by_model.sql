-- A6, the same by model: in-stock offers a day over the first and the last analysis_days days of the period (a day
-- without offers counts as zero), the change, and the model's share at the end.

with w as ({{ analysis_window() }}),

day_counts as (
    select
        count(distinct m.snapshot_date) filter (where m.snapshot_date <= w.first_window_end) as first_days,
        count(distinct m.snapshot_date) filter (where m.snapshot_date >= w.window_start) as last_days
    from {{ ref('daily_market') }} m
    cross join w
    where m.snapshot_date between w.start_day and w.end_day
),

models as (
    select
        s.generation,
        s.model,
        min(s.release_date) as release_date,
        sum(s.offer_count) filter (where s.snapshot_date <= w.first_window_end) as first_offers,
        sum(s.offer_count) filter (where s.snapshot_date >= w.window_start) as last_offers
    from {{ ref('daily_segment_prices') }} s
    cross join w
    where s.snapshot_date between w.start_day and w.end_day
    group by s.generation, s.model
)

select
    m.generation,
    m.model,
    round(coalesce(m.first_offers, 0)::numeric / c.first_days, 1) as first_window_offers_a_day,
    round(coalesce(m.last_offers, 0)::numeric / c.last_days, 1) as last_window_offers_a_day,
    round(100 * (coalesce(m.last_offers, 0)::numeric / c.last_days)
        / nullif(m.first_offers::numeric / c.first_days, 0) - 100, 1) as change_pct,
    round(100.0 * coalesce(m.last_offers, 0) / sum(m.last_offers) over (), 1) as last_window_share_pct
from models m
cross join day_counts c
order by m.release_date desc nulls last, m.model
