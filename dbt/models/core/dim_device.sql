-- One row per model and storage size, with what the analyses compare devices by: generation, series, age and the
-- launch prices (A1).

select
    device_id,
    model,
    storage_gb,
    generation,
    series,
    release_date,
    us_launch_price_usd,
    tr_launch_price_tl,
    -- the model's entry price and the size's rank within the model
    min(us_launch_price_usd) over (partition by model) as model_us_launch_price_usd,
    rank() over (partition by model order by storage_gb) as storage_rank,
    count(*) over (partition by model) as storage_options,
    source_url,
    tr_source_url
from {{ ref('stg_devices') }}
