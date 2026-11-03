-- Platform condition labels with their level on the shared scale (ref.condition_map, plan 9).

select
    platform || ':' || condition_raw as condition_id,
    platform,
    condition_raw,
    condition_level,
    condition_common,
    note
from {{ ref('condition_map') }}
