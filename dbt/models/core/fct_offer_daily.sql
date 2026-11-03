-- The cleaned daily offer (plan 8.2): one row per offer and day with its device, condition, seller and segment, and
-- its price in lira and in dollars. Kept as long as the raw detail (90 days). The last lookback_days days are computed
-- again on every run: a later run can add offers to them, and a day's dollar price is final only once its rate is.

{{ config(
    materialized='incremental',
    unique_key=['snapshot_date', 'offer_key'],
    incremental_strategy='delete+insert',
    on_schema_change='fail',
) }}

with offers as (
    select * from {{ ref('stg_offer_snapshots') }}
    {% if is_incremental() %}
    where snapshot_date >= {{ lookback_start('snapshot_date') }}
    {% endif %}
),

-- The rate of snapshot day D is the last one TCMB announced before D (plan 5.4). It is final once a later bulletin is
-- stored: before that a bulletin announced on the day before D may still be missing.
priced as (
    select
        o.*,
        fx.usd_try,
        fx.announced_on as fx_announced_on,
        fx.valid_to is not null as fx_confirmed
    from offers o
    left join {{ ref('stg_fx_rates') }} fx
        on o.snapshot_date >= fx.valid_from
        and (fx.valid_to is null or o.snapshot_date <= fx.valid_to)
)

select
    p.snapshot_date,
    p.offer_key,
    p.platform,
    p.variant_id,
    p.device_id,
    p.model,
    p.storage_gb,
    d.generation,
    d.series,
    d.release_date,
    p.snapshot_date - d.release_date as device_age_days,
    p.color,
    p.sim_type,
    p.condition_raw,
    p.condition_id,
    c.condition_level,
    c.condition_common,
    '{{ var("segment_condition") }}' as segment_basis,
    {{ segment_condition('p.condition_raw', 'c.condition_common') }} as segment_condition,
    p.device_id || ' · ' || {{ segment_condition('p.condition_raw', 'c.condition_common') }} as segment_id,
    p.seller_key,
    p.seller_id,
    coalesce(p.seller_is_platform, false) as seller_is_platform,
    p.is_buybox,
    p.price_tl,
    p.list_price_tl,
    p.promo_pct,
    p.usd_try,
    p.fx_announced_on,
    p.fx_confirmed,
    round(p.price_tl / p.usd_try, 2) as price_usd,
    round(p.list_price_tl / p.usd_try, 2) as list_price_usd,
    d.us_launch_price_usd,
    round(p.price_tl / p.usd_try / d.us_launch_price_usd, 4) as price_to_us_launch,
    p.stock_qty,
    p.in_stock,
    p.offer_url,
    p.run_id,
    p.fetched_at
from priced p
left join {{ ref('dim_device') }} d on d.device_id = p.device_id
left join {{ ref('dim_condition') }} c on c.condition_id = p.condition_id
