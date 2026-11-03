-- Refurbished iPhone price index, fixed basket, first draft (plan 10.3), in lira and dollars; 100 = the base period.
--
-- A day's level is the weighted mean of its basket segments' price relatives (median / base price) over the segments
-- priced that day. From one day to the next the index moves by the change of that mean over the segments priced on
-- both days, so a segment missing on a day neither breaks nor biases the series: its weight goes to the others
-- (chain linking). With every segment priced on every day this is exactly the fixed-basket formula.
--
-- The whole series follows from the permanent daily_segment_prices. A run writes the last lookback_days days and
-- keeps the older ones, unless the basket differs from the one the stored rows were computed with (its base period
-- was still being computed again, or its settings changed): then it writes every day. Each row carries the checksum of
-- its basket, and a test fails if stored rows of an older basket remain (docs/reprocessing.md).

{{ config(
    unique_key=['snapshot_date', 'platform'],
    incremental_strategy='delete+insert',
    on_schema_change='fail',
) }}

with relatives as (
    select
        p.platform,
        p.snapshot_date,
        p.segment_id,
        b.weight,
        p.median_price_tl / b.base_price_tl as relative_tl,
        p.median_price_usd / b.base_price_usd as relative_usd,
        p.fx_confirmed
    from {{ ref('daily_segment_prices') }} p
    join {{ ref('price_index_basket') }} b using (platform, segment_id)
    where p.snapshot_date >= date '{{ var("index_base_start") }}'
),

days as (
    select
        platform,
        snapshot_date,
        lag(snapshot_date) over (partition by platform order by snapshot_date) as previous_date,
        count(*) as segments_priced,
        sum(weight) as weight_priced,
        sum(weight * relative_tl) / sum(weight) as level_tl,
        sum(weight * relative_usd) / sum(weight) as level_usd,
        bool_and(fx_confirmed) as fx_confirmed
    from relatives
    group by platform, snapshot_date
),

links as (
    select
        d.platform,
        d.snapshot_date,
        count(*) as segments_matched,
        sum(c.weight * c.relative_tl) / sum(c.weight * p.relative_tl) as link_tl,
        sum(c.weight * c.relative_usd) / sum(c.weight * p.relative_usd) as link_usd
    from days d
    join relatives c on c.platform = d.platform and c.snapshot_date = d.snapshot_date
    join relatives p
        on p.platform = d.platform and p.snapshot_date = d.previous_date and p.segment_id = c.segment_id
    group by d.platform, d.snapshot_date
),

series as (
    select
        d.platform,
        d.snapshot_date,
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

basket as (
    select
        platform,
        count(*) as basket_segments,
        bool_and(final) as basket_final,
        md5(string_agg(
            segment_id || ':' || weight || ':' || base_price_tl || ':' || base_price_usd, '|' order by segment_id
        )) as basket_checksum
    from {{ ref('price_index_basket') }}
    group by platform
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
    b.basket_checksum,
    s.fx_confirmed,
    now() as computed_at
from series s
join basket b using (platform)
{% if is_incremental() %}
where s.snapshot_date >= {{ lookback_start('snapshot_date') }}
    or exists (
        select 1 from {{ this }} t where t.platform = s.platform and t.basket_checksum <> b.basket_checksum
    )
{% endif %}
