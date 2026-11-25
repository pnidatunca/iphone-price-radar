-- Refurbished iPhone price index (plan 10.3), in lira and dollars; 100 = the base period's prices.
--
-- Each day is priced with the basket in effect on it (price_index_basket): the base period's until
-- index_renewal_start, then each month's own. A day's level is the weighted mean of its basket segments' price
-- relatives (median / reference price) over the segments priced that day. From one day to the next the index moves by
-- the change of that mean over the segments priced on both days, both days measured with the later day's basket
-- (chain linking). So a segment missing on a day neither breaks nor biases the series: its weight goes to the others.
-- And on the first day of a new basket the index carries on from the day before:
-- index_t = index_t-1 × (new basket_t / new basket_t-1). With one basket and every segment priced on every day this is
-- exactly the fixed-basket formula.
--
-- The whole series follows from the permanent daily_segment_prices. A run writes the last lookback_days days and
-- keeps the older ones, unless the baskets differ from those the stored rows were computed with (a new month's basket,
-- one whose reference period was still being computed again, or changed settings): then it writes every day. Each row
-- carries the checksum of the baskets, and a test fails if stored rows of older baskets remain (docs/reprocessing.md).

{{ config(
    unique_key=['snapshot_date', 'platform'],
    incremental_strategy='delete+insert',
    on_schema_change='fail',
) }}

with baskets as (
    select
        platform,
        basket_start,
        coalesce(basket_end, date '9999-12-31') as basket_end,
        count(*) as basket_segments,
        bool_and(final) as basket_final
    from {{ ref('price_index_basket') }}
    group by platform, basket_start, basket_end
),

relatives as (
    -- the days of each basket, and of the month before it: its first day is linked to the day before with its own
    -- reference prices
    select
        p.platform,
        p.snapshot_date,
        b.basket_start,
        p.segment_id,
        b.weight,
        p.median_price_tl / b.ref_price_tl as relative_tl,
        p.median_price_usd / b.ref_price_usd as relative_usd,
        p.fx_confirmed
    from {{ ref('daily_segment_prices') }} p
    join {{ ref('price_index_basket') }} b using (platform, segment_id)
    where p.snapshot_date >= date '{{ var("index_base_start") }}'
        and p.snapshot_date between b.basket_start - 31 and coalesce(b.basket_end, date '9999-12-31')
),

days as (
    -- each day with the basket in effect on it
    select
        r.platform,
        r.snapshot_date,
        r.basket_start,
        lag(r.snapshot_date) over (partition by r.platform order by r.snapshot_date) as previous_date,
        count(*) as segments_priced,
        sum(r.weight) as weight_priced,
        sum(r.weight * r.relative_tl) / sum(r.weight) as level_tl,
        sum(r.weight * r.relative_usd) / sum(r.weight) as level_usd,
        bool_and(r.fx_confirmed) as fx_confirmed
    from relatives r
    join baskets b using (platform, basket_start)
    where r.snapshot_date between b.basket_start and b.basket_end
    group by r.platform, r.snapshot_date, r.basket_start
),

links as (
    select
        d.platform,
        d.snapshot_date,
        count(*) as segments_matched,
        sum(c.weight * c.relative_tl) / sum(c.weight * p.relative_tl) as link_tl,
        sum(c.weight * c.relative_usd) / sum(c.weight * p.relative_usd) as link_usd
    from days d
    join relatives c
        on c.platform = d.platform and c.basket_start = d.basket_start and c.snapshot_date = d.snapshot_date
    join relatives p
        on p.platform = d.platform and p.basket_start = d.basket_start and p.snapshot_date = d.previous_date
        and p.segment_id = c.segment_id
    group by d.platform, d.snapshot_date
),

series as (
    select
        d.platform,
        d.snapshot_date,
        d.basket_start,
        d.segments_priced,
        l.segments_matched,
        d.weight_priced,
        l.link_tl,
        l.link_usd,
        -- the first day's level, carried on by the product of the daily links (the first day has none)
        100 * first_value(d.level_tl) over chain * exp(coalesce(sum(ln(l.link_tl)) over chain, 0)) as index_tl,
        100 * first_value(d.level_usd) over chain * exp(coalesce(sum(ln(l.link_usd)) over chain, 0)) as index_usd,
        d.fx_confirmed
    from days d
    left join links l using (platform, snapshot_date)
    window chain as (partition by d.platform order by d.snapshot_date rows between unbounded preceding and current row)
),

checksums as (
    {{ index_basket_checksums() }}
)

select
    s.snapshot_date,
    s.platform,
    round(index_tl::numeric, 3) as index_tl,
    round(index_usd::numeric, 3) as index_usd,
    segments_priced,
    segments_matched,
    b.basket_segments,
    round(weight_priced::numeric, 4) as weight_priced,
    round(link_tl::numeric, 6) as link_tl,
    round(link_usd::numeric, 6) as link_usd,
    date '{{ var("index_base_start") }}' as base_start,
    date '{{ var("index_base_end") }}' as base_end,
    b.basket_final,
    c.basket_checksum,
    s.fx_confirmed,
    now() as computed_at
from series s
join baskets b using (platform, basket_start)
join checksums c using (platform)
{% if is_incremental() %}
where s.snapshot_date >= {{ lookback_start('snapshot_date') }}
    or exists (
        select 1 from {{ this }} t where t.platform = s.platform and t.basket_checksum <> c.basket_checksum
    )
{% endif %}
