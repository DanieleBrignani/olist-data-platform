{#
  Use the configured schema name verbatim (stg, int, warehouse_build, marts_build) instead of
  dbt's default "<target_schema>_<custom_schema>". The publish step (ADR-0005) renames
  warehouse_build -> warehouse and marts_build -> marts, so the names must be predictable.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {{ custom_schema_name | trim if custom_schema_name is not none else target.schema }}
{%- endmacro %}
