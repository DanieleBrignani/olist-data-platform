-- Grain: one row per seller with at least one sale.
select
    s.seller_id,
    s.state                                                             as seller_state,
    s.city                                                              as seller_city,
    p.first_sale_at::date                                               as first_sale_date,
    p.last_sale_at::date                                                as last_sale_date,
    p.order_count,
    p.items_sold,
    p.revenue,
    p.freight_value,
    p.canceled_orders,
    p.delivered_orders,
    p.late_orders,
    round(p.late_orders::numeric / nullif(p.delivered_orders, 0), 4)    as late_rate,
    p.orders_shipped_after_limit,
    round(p.orders_shipped_after_limit::numeric / p.order_count, 4)     as shipped_after_limit_rate,
    p.avg_days_to_carrier,
    p.avg_review_score,
    rank() over (order by p.revenue desc)                               as revenue_rank
from {{ ref('dim_seller') }} as s
join {{ ref('int_seller_performance') }} as p using (seller_id)
