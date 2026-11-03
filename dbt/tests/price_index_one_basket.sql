-- Every stored index day was computed with today's basket. The index rewrites its days when the basket changes; a day
-- that remains from an older basket (e.g. before a later base start) needs a rebuild (docs/reprocessing.md).

with basket as (
    select
        platform,
        md5(string_agg(
            segment_id || ':' || weight || ':' || base_price_tl || ':' || base_price_usd, '|' order by segment_id
        )) as basket_checksum
    from {{ ref('price_index_basket') }}
    group by platform
)

select i.snapshot_date, i.platform, i.basket_checksum
from {{ ref('price_index') }} i
left join basket b using (platform)
where b.basket_checksum is distinct from i.basket_checksum
    or i.snapshot_date < date '{{ var("index_base_start") }}'
