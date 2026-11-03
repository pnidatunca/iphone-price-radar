-- The permanent marts hold segments of one basis only, the one var('segment_condition') names. Changing the basis
-- needs a rebuild of the marts (docs/reprocessing.md); until then this test fails.

select 'daily_segment_prices' as model, segment_basis, count(*) as rows
from {{ ref('daily_segment_prices') }}
where segment_basis <> '{{ var("segment_condition") }}'
group by segment_basis
