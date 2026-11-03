-- One row per offer and day with the whole offer rebuilt. raw.offer_snapshots stores the payload only on the day an
-- offer changed, so every other day takes the payload of the offer's latest earlier row that has one. Payload details
-- outside payload_hash (seller score, delivery days) are therefore as of the offer's last change.

with snapshots as (
    select
        *,
        -- the rows of an offer from one stored payload up to the next share a group
        count(payload) over (partition by offer_key order by snapshot_date) as payload_group
    from {{ source('raw', 'offer_snapshots') }}
),

carried as (
    select
        *,
        first_value(payload) over payload_rows as full_payload,
        first_value(snapshot_date) over payload_rows as payload_date
    from snapshots
    window payload_rows as (partition by offer_key, payload_group order by snapshot_date)
),

prices as (
    select
        *,
        -- Rows stored before list_price_tl existed (parser getmobil-1, until 19.10.2026) keep the struck-through
        -- price in the payload. It is used under the rule of the adapters (K24): only above the price paid and for a
        -- discount of at most 50%.
        case
            when list_price_tl is not null then list_price_tl
            when {{ json_number("full_payload -> 'seller' -> 'strikethrough_price'") }} > price_tl
                and price_tl >= 0.5 * {{ json_number("full_payload -> 'seller' -> 'strikethrough_price'") }}
                then {{ json_number("full_payload -> 'seller' -> 'strikethrough_price'") }}
            else price_tl
        end as full_list_price_tl
    from carried
)

select
    snapshot_date,
    offer_key,
    platform,
    run_id,
    fetched_at,
    split_part(offer_key, ':', 2)::bigint as variant_id,
    model,
    storage_gb,
    model || ' ' || storage_gb || ' GB' as device_id,
    color,
    sim_type,
    condition_raw,
    platform || ':' || condition_raw as condition_id,
    condition_flags,
    battery_raw,
    warranty_raw,
    sku,
    seller_id,
    platform || ':' || seller_id as seller_key,
    {{ json_boolean("full_payload -> 'seller' -> 'is_getmobil'") }} as seller_is_platform,
    {{ json_number("full_payload -> 'seller' -> 'score'") }} as seller_score,
    {{ json_boolean("full_payload -> 'seller' -> 'top_seller'") }} as seller_top,
    {{ json_boolean("full_payload -> 'seller' -> 'trusted_seller'") }} as seller_trusted,
    {{ json_boolean("full_payload -> 'seller' -> 'fast_delivery'") }} as fast_delivery,
    {{ json_number("full_payload -> 'seller' -> 'shipping_days'") }}::integer as shipping_days,
    {{ json_number("full_payload -> 'seller' -> 'battery_addon_price'") }} as battery_addon_price_tl,
    is_buybox,
    price_tl,
    full_list_price_tl as list_price_tl,
    case
        when promo_pct is not null then promo_pct
        else round((full_list_price_tl - price_tl) / full_list_price_tl * 100, 2)
    end as promo_pct,
    list_price_tl is null as list_price_from_payload,
    stock_qty,
    in_stock,
    product_url,
    offer_url,
    payload is not null as payload_changed,
    payload_date,
    full_payload as payload,
    payload_hash,
    parser_version
from prices
