{#
  Grain test for composite keys: no two rows share the same combination of `columns`.
  Written in-project instead of adding the dbt_utils package: one small macro does not
  justify a network dependency at `dbt deps` time.
#}
{% test unique_combination_of_columns(model, columns) %}
select {{ columns | join(', ') }}, count(*) as rows_with_key
from {{ model }}
group by {{ columns | join(', ') }}
having count(*) > 1
{% endtest %}
