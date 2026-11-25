-- Every day of the index but the first is linked to the day before it, over at least one segment priced on both days
-- with the day's basket. An unlinked day would carry the index on unchanged; it can follow a gap of more than a month
-- in the data, which the first day of a basket cannot be linked across.

select i.snapshot_date, i.platform
from {{ ref('price_index') }} i
where i.link_tl is null
    and i.snapshot_date > (select min(f.snapshot_date) from {{ ref('price_index') }} f where f.platform = i.platform)
