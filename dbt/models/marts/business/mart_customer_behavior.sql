-- Grain: one row per customer (person = customer_unique_id).
select
    c.customer_unique_id,
    c.state,
    c.first_order_date,
    co.last_order_at::date                                          as last_order_date,
    co.order_count,
    co.valid_order_count,
    c.is_repeat_customer,
    coalesce(co.lifetime_value, 0)                                  as lifetime_value,
    round(coalesce(co.lifetime_value, 0) / nullif(co.valid_order_count, 0), 2)
                                                                    as avg_order_value,
    co.avg_review_score,
    co.last_order_at::date - co.first_order_at::date                as days_first_to_last_order
from {{ ref('dim_customer') }} as c
join {{ ref('int_customer_orders') }} as co using (customer_unique_id)
