-- The merged figures of an offer's life stay consistent with each other after every incremental run.

select *
from {{ ref('offer_lifecycle') }}
where first_seen > last_seen
    or days_seen < 1
    or days_seen > last_seen - first_seen + 1
    or min_price_tl > least(first_price_tl, last_price_tl)
    or max_price_tl < greatest(first_price_tl, last_price_tl)
    or price_change_count > days_seen - 1
    or price_cut_count > price_change_count
