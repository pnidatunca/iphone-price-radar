{#- Layers are schemas of their own (staging, core, marts, ref), not prefixed with the target schema: there is one
    database and one target, and readers such as Power BI see plain names. -#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {{ custom_schema_name if custom_schema_name is not none else target.schema }}
{%- endmacro %}
