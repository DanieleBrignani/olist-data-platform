-- Grain: one row per purchase month.
-- GMV = item price + freight of orders NOT canceled/unavailable. AOV = GMV / such orders
-- that have at least one item. Payment volume = sum of payments (all orders), which can
-- differ from GMV (vouchers, instalment interest, canceled orders being charged).
with orders as (
    select d.month_start, f.*
    from {{ ref('fct_orders') }} as f
    join {{ ref('dim_date') }} as d on d.date_key = f.purchase_date_key
)

select
    month_start,
    to_char(month_start, 'YYYY-MM')                                         as year_month,
    count(*)                                                                as orders,
    count(*) filter (where is_canceled)                                     as canceled_orders,
    round(count(*) filter (where is_canceled)::numeric / count(*), 4)       as cancellation_rate,
    count(*) filter (where is_delivered)                                    as delivered_orders,
    sum(item_count) filter (where not is_canceled)                          as items_sold,
    sum(items_value) filter (where not is_canceled)                         as item_revenue,
    sum(freight_value) filter (where not is_canceled)                       as freight_revenue,
    coalesce(sum(order_value) filter (where not is_canceled), 0)            as gmv,
    round(
        sum(order_value) filter (where not is_canceled and item_count > 0)
        / nullif(count(*) filter (where not is_canceled and item_count > 0), 0), 2
    )                                                                       as avg_order_value,
    sum(payment_value)                                                      as payment_volume,
    count(distinct customer_unique_id)                                      as active_customers
from orders
group by month_start
