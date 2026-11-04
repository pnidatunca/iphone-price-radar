{% macro analysis_window() %}
    {#- The days an analysis covers, as one row: the whole period from the first day built to analysis_end (empty: the
        latest day built), the cross-sectional window of its last analysis_days days, and the first analysis_days
        days, which comparisons of the start and the end use. The two windows overlap when the period is shorter
        than two windows. -#}
    select
        start_day,
        end_day,
        end_day - {{ var('analysis_days') }} + 1 as window_start,
        least(start_day + {{ var('analysis_days') }} - 1, end_day) as first_window_end
    from (
        select
            min(snapshot_date) as start_day,
            {%- if var('analysis_end') %}
            date '{{ var("analysis_end") }}' as end_day
            {%- else %}
            max(snapshot_date) as end_day
            {%- endif %}
        from {{ ref('daily_market') }}
    ) period
{% endmacro %}
