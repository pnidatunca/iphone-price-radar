-- Every stored index day was computed with today's baskets. The index writes all its days again when a basket
-- changes; a day that remains from older baskets (e.g. before a later base start) needs a rebuild (docs/reprocessing.md).

with checksums as (
    {{ index_basket_checksums() }}
)

select i.snapshot_date, i.platform, i.basket_checksum
from {{ ref('price_index') }} i
left join checksums c using (platform)
where c.basket_checksum is distinct from i.basket_checksum
    or i.snapshot_date < date '{{ var("index_base_start") }}'
