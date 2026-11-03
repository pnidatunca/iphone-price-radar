-- USD/TRY per TCMB bulletin with the days it serves (plan 5.4): a bulletin applies from the day after its
-- announcement (valid_from) until the next one takes over. valid_to is empty for the latest bulletin, whose last day is
-- not known yet.

select
    announced_on,
    valid_from,
    lead(valid_from) over (order by announced_on) - 1 as valid_to,
    bulletin_no,
    forex_selling / unit as usd_try
from {{ source('ref', 'fx_rates') }}
where currency = 'USD'
