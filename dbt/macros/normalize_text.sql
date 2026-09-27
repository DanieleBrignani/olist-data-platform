{#
  City names in the source mix accents and casing ("São Paulo", "sao paulo").
  unaccent would need a superuser-created extension, which the least-privilege roles cannot
  install, so the Portuguese accents present in the data are mapped explicitly.
#}
{% macro normalize_city(column) -%}
    translate(lower(trim({{ column }})), 'áàâãäéèêëíìîïóòôõöúùûüç', 'aaaaaeeeeiiiiooooouuuuc')
{%- endmacro %}

{#- Seconds between two timestamps expressed in (fractional) days; NULL-safe -#}
{% macro days_between(start_ts, end_ts) -%}
    round((extract(epoch from ({{ end_ts }} - {{ start_ts }})) / 86400.0)::numeric, 2)
{%- endmacro %}

{#- Integer yyyymmdd key into dim_date; NULL when the timestamp is NULL -#}
{% macro date_key(ts) -%}
    (to_char({{ ts }}, 'YYYYMMDD'))::integer
{%- endmacro %}
