{{ config(severity='warn', meta={'dq_severity': 'WARNING', 'model': 'mart_sales'}) }}
{#
  WARNING: a month whose order count is below 10% of the average of the previous 3 months.
  On Olist v2 this flags the edges of the extract (2016-12, 2018-09, 2018-10): real but
  incomplete periods that must be called out in reporting, not silently charted.
#}
with volume as (
    select
        year_month,
        orders,
        avg(orders) over (order by month_start rows between 3 preceding and 1 preceding)
            as trailing_avg_orders
    from {{ ref('mart_sales') }}
)

select year_month, orders, round(trailing_avg_orders, 1) as trailing_avg_orders
from volume
where trailing_avg_orders is not null
  and orders < 0.1 * trailing_avg_orders
