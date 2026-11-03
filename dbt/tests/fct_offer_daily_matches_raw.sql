-- Every raw offer row of a day the fact holds is in the fact, once: no offer is lost or doubled on the way.

with raw_days as (
    select snapshot_date, count(*) as raw_rows
    from {{ source('raw', 'offer_snapshots') }}
    group by snapshot_date
),

fact_days as (
    select snapshot_date, count(*) as fact_rows
    from {{ ref('fct_offer_daily') }}
    group by snapshot_date
)

select f.snapshot_date, f.fact_rows, r.raw_rows
from fact_days f
left join raw_days r using (snapshot_date)
where r.raw_rows is distinct from f.fact_rows
