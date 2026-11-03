-- One row per offer over its whole life (plan 8.2, 8.3): first and last day, first and last price, price changes and
-- days seen. Permanent: the raw detail is deleted after 90 days, so these figures cannot be computed again from it.
-- Each run merges the offer's new days into its row: first day and first price stay, the rest moves on. A day is new
-- for an offer when it is after the offer's last_seen, so offers a fallback run adds to a day already processed are
-- still counted (within lookback_days).

{{ config(
    unique_key='offer_key',
    incremental_strategy='delete+insert',
    on_schema_change='fail',
) }}

with new_days as (
    select
        f.*,
        lag(f.price_tl) over (partition by f.offer_key order by f.snapshot_date) as previous_price_tl
    from {{ ref('fct_offer_daily') }} f
    {% if is_incremental() %}
    left join {{ this }} t on t.offer_key = f.offer_key
    where f.snapshot_date >= {{ lookback_start('last_seen') }}
        and (t.last_seen is null or f.snapshot_date > t.last_seen)
    {% endif %}
),

batch as (
    select
        offer_key,
        min(platform) as platform,
        min(variant_id) as variant_id,
        min(seller_key) as seller_key,
        bool_or(seller_is_platform) as seller_is_platform,
        min(device_id) as device_id,
        min(model) as model,
        min(storage_gb) as storage_gb,
        min(color) as color,
        min(sim_type) as sim_type,
        min(condition_raw) as condition_raw,
        min(segment_id) as segment_id,
        min(snapshot_date) as first_seen,
        max(snapshot_date) as last_seen,
        (array_agg(price_tl order by snapshot_date))[1] as first_price_tl,
        (array_agg(price_tl order by snapshot_date desc))[1] as last_price_tl,
        min(price_tl) as min_price_tl,
        max(price_tl) as max_price_tl,
        count(*) filter (where previous_price_tl is not null and price_tl <> previous_price_tl) as changes,
        count(*) filter (where previous_price_tl is not null and price_tl < previous_price_tl) as cuts,
        count(*) as days
    from new_days
    group by offer_key
)

select
    b.offer_key,
{% if is_incremental() %}
    coalesce(t.platform, b.platform) as platform,
    coalesce(t.variant_id, b.variant_id) as variant_id,
    coalesce(t.seller_key, b.seller_key) as seller_key,
    coalesce(t.seller_is_platform, b.seller_is_platform) as seller_is_platform,
    coalesce(t.device_id, b.device_id) as device_id,
    coalesce(t.model, b.model) as model,
    coalesce(t.storage_gb, b.storage_gb) as storage_gb,
    coalesce(t.color, b.color) as color,
    coalesce(t.sim_type, b.sim_type) as sim_type,
    coalesce(t.condition_raw, b.condition_raw) as condition_raw,
    coalesce(t.segment_id, b.segment_id) as segment_id,
    coalesce(t.first_seen, b.first_seen) as first_seen,
    b.last_seen,
    coalesce(t.days_seen, 0) + b.days as days_seen,
    coalesce(t.first_price_tl, b.first_price_tl) as first_price_tl,
    b.last_price_tl,
    least(t.min_price_tl, b.min_price_tl) as min_price_tl,
    greatest(t.max_price_tl, b.max_price_tl) as max_price_tl,
    -- a change between the stored last price and the batch's first price counts too
    coalesce(t.price_change_count, 0) + b.changes
        + case when t.last_price_tl is not null and t.last_price_tl <> b.first_price_tl then 1 else 0 end
        as price_change_count,
    coalesce(t.price_cut_count, 0) + b.cuts
        + case when t.last_price_tl is not null and b.first_price_tl < t.last_price_tl then 1 else 0 end
        as price_cut_count,
{% else %}
    b.platform,
    b.variant_id,
    b.seller_key,
    b.seller_is_platform,
    b.device_id,
    b.model,
    b.storage_gb,
    b.color,
    b.sim_type,
    b.condition_raw,
    b.segment_id,
    b.first_seen,
    b.last_seen,
    b.days as days_seen,
    b.first_price_tl,
    b.last_price_tl,
    b.min_price_tl,
    b.max_price_tl,
    b.changes as price_change_count,
    b.cuts as price_cut_count,
{% endif %}
    now() as updated_at
from batch b
{% if is_incremental() %}
left join {{ this }} t on t.offer_key = b.offer_key
{% endif %}
