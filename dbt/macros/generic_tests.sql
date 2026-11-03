{#- Generic tests of the project. Each returns the rows that break the rule. -#}

{#- No two rows share the values of these columns. -#}
{% test unique_combination(model, columns) %}
    select {{ columns | join(', ') }}, count(*) as rows
    from {{ model }}
    group by {{ columns | join(', ') }}
    having count(*) > 1
{% endtest %}

{#- Every row satisfies a SQL condition (on a model or one of its columns). -#}
{% test expression_is_true(model, expression, column_name=none) %}
    select *
    from {{ model }}
    where not ({{ expression }})
{% endtest %}
