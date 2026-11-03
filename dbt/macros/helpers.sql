{#- A JSON number or boolean of a payload as SQL; anything else (missing, null, a string) gives null, so an unexpected
    value in one payload cannot break the view. -#}
{% macro json_number(expression) -%}
    case when jsonb_typeof({{ expression }}) = 'number' then ({{ expression }})::text::numeric end
{%- endmacro %}

{% macro json_boolean(expression) -%}
    case when jsonb_typeof({{ expression }}) = 'boolean' then ({{ expression }})::text::boolean end
{%- endmacro %}

{#- The first day an incremental model computes again: lookback_days before the latest day it holds. -#}
{% macro lookback_start(date_column) -%}
    (select coalesce(max({{ date_column }}), date '1900-01-01') - {{ var('lookback_days') }} from {{ this }})
{%- endmacro %}

{#- The segment of an offer (plan 8.2): its device and its condition on the basis var('segment_condition') names. -#}
{% macro segment_condition(raw_column, common_column) -%}
    {%- set basis = var('segment_condition') -%}
    {%- if basis == 'raw' -%}
        {{ raw_column }}
    {%- elif basis == 'common' -%}
        coalesce({{ common_column }}, {{ raw_column }})
    {%- else -%}
        {{ exceptions.raise_compiler_error("var segment_condition must be 'raw' or 'common', not '" ~ basis ~ "'") }}
    {%- endif -%}
{%- endmacro %}

{#- Grant the Streamlit app's read-only role select on the marts (plan 13), when the role and the schema exist. -#}
{% macro grant_reader() %}
    {%- set role = var('reader_role') -%}
    {%- if execute and role -%}
        {%- set found = run_query(
            "select 1 from pg_roles where rolname = " ~ dbt.string_literal(role)
            ~ " and exists (select 1 from pg_namespace where nspname = 'marts')"
        ) -%}
        {%- if found.rows | length > 0 -%}
            grant usage on schema marts to {{ adapter.quote(role) }};
            grant select on all tables in schema marts to {{ adapter.quote(role) }};
        {%- else -%}
            select 1
        {%- endif -%}
    {%- else -%}
        select 1
    {%- endif -%}
{% endmacro %}
