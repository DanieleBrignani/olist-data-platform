{#
  Rows whose timestamp lies in the future relative to the build (invalid for a closed
  snapshot; would also distort time series and freshness).
#}
{% test not_in_future(model, column_name) %}
select *
from {{ model }}
where {{ column_name }} > localtimestamp
{% endtest %}
