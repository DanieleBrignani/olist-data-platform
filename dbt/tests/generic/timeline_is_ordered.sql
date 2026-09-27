{#
  Rows where an event timestamp precedes the one it must follow, e.g. delivery before
  purchase. NULLs never violate (a milestone that has not happened yet is not an error).
#}
{% test timeline_is_ordered(model, column_name, must_not_precede) %}
select *
from {{ model }}
where {{ column_name }} is not null
  and {{ must_not_precede }} is not null
  and {{ column_name }} < {{ must_not_precede }}
{% endtest %}
