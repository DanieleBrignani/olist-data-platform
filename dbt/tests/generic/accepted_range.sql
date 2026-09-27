{# Rows outside [min_value, max_value] (either bound optional; NULL never violates). #}
{% test accepted_range(model, column_name, min_value=none, max_value=none, inclusive=true) %}
{%- set lo = '>=' if inclusive else '>' -%}
{%- set hi = '<=' if inclusive else '<' -%}
select *
from {{ model }}
where {{ column_name }} is not null
  and not (
      true
      {% if min_value is not none %} and {{ column_name }} {{ lo }} {{ min_value }} {% endif %}
      {% if max_value is not none %} and {{ column_name }} {{ hi }} {{ max_value }} {% endif %}
  )
{% endtest %}
