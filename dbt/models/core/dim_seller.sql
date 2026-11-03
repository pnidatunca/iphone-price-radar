-- One row per seller: the platform and its numeric id only, never a name, slug or store link (K22). Dates and counts
-- cover the detail kept in raw.offer_snapshots (90 days); marts.offer_lifecycle keeps the full history per offer.

with offers as (
    select * from {{ ref('stg_offer_snapshots') }}
),

activity as (
    select
        seller_key,
        min(platform) as platform,
        min(seller_id) as seller_id,
        min(snapshot_date) as first_seen,
        max(snapshot_date) as last_seen,
        count(distinct snapshot_date) as days_seen,
        count(distinct offer_key) as offers_seen
    from offers
    group by seller_key
),

-- the freshest payload of the seller's offers on its latest day
latest as (
    select distinct on (seller_key)
        seller_key,
        seller_is_platform,
        seller_score,
        seller_top,
        seller_trusted
    from offers
    order by seller_key, snapshot_date desc, payload_date desc
)

select
    a.seller_key,
    a.platform,
    a.seller_id,
    coalesce(l.seller_is_platform, false) as is_platform,
    l.seller_score as latest_score,
    l.seller_top as latest_top,
    l.seller_trusted as latest_trusted,
    a.first_seen,
    a.last_seen,
    a.days_seen,
    a.offers_seen
from activity a
join latest l using (seller_key)
