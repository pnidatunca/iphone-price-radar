-- The device reference with the key the offers carry: model and storage size.

select
    model || ' ' || storage_gb || ' GB' as device_id,
    model,
    generation,
    series,
    release_date,
    storage_gb,
    us_launch_price_usd,
    tr_launch_price_tl,
    source_url,
    tr_source_url
from {{ ref('devices') }}
