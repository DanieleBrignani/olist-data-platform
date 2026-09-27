{{ config(meta={'dq_severity': 'CRITICAL', 'model': 'mart_sales'}) }}
{#
  CRITICAL: business marts must add up to the facts they summarise. Reports built on marts
  and ad-hoc queries on facts must never disagree.
#}
with checks as (
    select 'mart_sales.orders = fct_orders' as check_name,
           (select sum(orders) from {{ ref('mart_sales') }})::numeric as mart_value,
           (select count(*) from {{ ref('fct_orders') }})::numeric as fact_value
    union all
    select 'mart_sales.gmv = non-canceled order_value',
           (select sum(gmv) from {{ ref('mart_sales') }}),
           (select coalesce(sum(order_value), 0) from {{ ref('fct_orders') }} where not is_canceled)
    union all
    select 'mart_sales.payment_volume = fct_payments',
           (select sum(payment_volume) from {{ ref('mart_sales') }}),
           (select coalesce(sum(payment_value), 0) from {{ ref('fct_payments') }})
    union all
    select 'mart_payment_methods.payment_volume = fct_payments',
           (select sum(payment_volume) from {{ ref('mart_payment_methods') }}),
           (select coalesce(sum(payment_value), 0) from {{ ref('fct_payments') }})
    union all
    select 'mart_delivery_performance.delivered_orders = delivered facts',
           (select sum(delivered_orders) from {{ ref('mart_delivery_performance') }}),
           (select count(*) from {{ ref('fct_orders') }} where is_delivered)
    union all
    select 'mart_review_distribution.reviews = fct_reviews',
           (select sum(reviews) from {{ ref('mart_review_distribution') }}),
           (select count(*) from {{ ref('fct_reviews') }})
    union all
    select 'mart_customer_behavior rows = dim_customer',
           (select count(*) from {{ ref('mart_customer_behavior') }}),
           (select count(*) from {{ ref('dim_customer') }})
)

select *
from checks
where mart_value is distinct from fact_value
