-- One row per seller that sold at least one item
with seller_orders as (
    -- a seller's view of each order they took part in
    select
        i.seller_id,
        i.order_id,
        sum(i.price)                        as seller_items_value,
        sum(i.freight_value)                as seller_freight_value,
        count(*)                            as seller_items,
        bool_or(i.shipped_after_limit)      as shipped_after_limit
    from {{ ref('int_order_items_enriched') }} as i
    group by i.seller_id, i.order_id
)

select
    so.seller_id,
    count(*)                                                        as order_count,
    sum(so.seller_items)                                            as items_sold,
    sum(so.seller_items_value)                                      as revenue,
    sum(so.seller_freight_value)                                    as freight_value,
    min(o.purchased_at)                                             as first_sale_at,
    max(o.purchased_at)                                             as last_sale_at,
    count(*) filter (where o.is_canceled)                           as canceled_orders,
    count(*) filter (where o.is_delivered)                          as delivered_orders,
    count(*) filter (where o.is_late)                               as late_orders,
    count(*) filter (where so.shipped_after_limit)                  as orders_shipped_after_limit,
    round(avg(o.avg_review_score), 2)                               as avg_review_score,
    round(avg(o.purchase_to_carrier_days), 2)                       as avg_days_to_carrier
from seller_orders as so
join {{ ref('int_order_enriched') }} as o using (order_id)
group by so.seller_id
