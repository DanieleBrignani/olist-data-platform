{{ config(meta={'dq_severity': 'CRITICAL', 'model': 'fct_orders'}) }}
{#
  CRITICAL reconciliation: the warehouse must contain exactly what src contains - same row
  counts per grain and the same money to the cent. A mismatch means a transformation dropped,
  duplicated or rounded business data. Returns one row per failing check.
#}
with checks as (
    select 'orders: row count' as check_name,
           (select count(*) from {{ source('src', 'orders') }})::numeric as src_value,
           (select count(*) from {{ ref('fct_orders') }})::numeric as warehouse_value
    union all
    select 'order_items: row count',
           (select count(*) from {{ source('src', 'order_items') }}),
           (select count(*) from {{ ref('fct_order_items') }})
    union all
    select 'payments: row count',
           (select count(*) from {{ source('src', 'order_payments') }}),
           (select count(*) from {{ ref('fct_payments') }})
    union all
    select 'reviews: row count',
           (select count(*) from {{ source('src', 'order_reviews') }}),
           (select count(*) from {{ ref('fct_reviews') }})
    union all
    select 'items: sum(price)',
           (select sum(price) from {{ source('src', 'order_items') }}),
           (select sum(price) from {{ ref('fct_order_items') }})
    union all
    select 'items: sum(freight_value)',
           (select sum(freight_value) from {{ source('src', 'order_items') }}),
           (select sum(freight_value) from {{ ref('fct_order_items') }})
    union all
    select 'payments: sum(payment_value)',
           (select sum(payment_value) from {{ source('src', 'order_payments') }}),
           (select sum(payment_value) from {{ ref('fct_payments') }})
    union all
    select 'orders: sum(order_value) = items total',
           (select sum(price + freight_value) from {{ source('src', 'order_items') }}),
           (select sum(order_value) from {{ ref('fct_orders') }})
    union all
    -- persons WITH an order: a customer whose only order was quarantined stays in src but
    -- legitimately has no place in dim_customer
    select 'customers: distinct persons with orders',
           (select count(distinct c.customer_unique_id)
              from {{ source('src', 'customers') }} as c
              join {{ source('src', 'orders') }} as o using (customer_id)),
           (select count(*) from {{ ref('dim_customer') }})
)

select *
from checks
where src_value is distinct from warehouse_value
