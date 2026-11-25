-- The base period and every month from index_renewal_start up to the latest day have a basket. A month whose
-- reference month (the one before it) had no segment that met the rules would leave its days out of the index.

with latest as (
    select platform, max(snapshot_date) as latest_day
    from {{ ref('daily_segment_prices') }}
    group by platform
),

expected as (
    select platform, date '{{ var("index_base_start") }}' as basket_start
    from latest
    union all
    select l.platform, m.month_start::date
    from latest l
    cross join lateral generate_series(
        date '{{ var("index_renewal_start") }}'::timestamp, l.latest_day::timestamp, interval '1 month'
    ) as m (month_start)
)

select e.platform, e.basket_start
from expected e
where not exists (
    select 1 from {{ ref('price_index_basket') }} b where b.platform = e.platform and b.basket_start = e.basket_start
)
