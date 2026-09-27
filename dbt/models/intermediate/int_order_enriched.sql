-- One row per order with everything the order fact and the marts need
with items as (
    select
        order_id,
        count(*)            as item_count,
        count(distinct seller_id) as seller_count,
        sum(price)          as items_value,
        sum(freight_value)  as freight_value
    from {{ ref('stg_order_items') }}
    group by order_id
)

select
    o.order_id,
    o.customer_id,
    c.customer_unique_id,
    c.zip_code_prefix                           as customer_zip_code_prefix,
    c.state                                     as customer_state,
    o.order_status,
    o.is_canceled,
    o.purchased_at,
    o.approved_at,
    o.delivered_to_carrier_at,
    o.delivered_to_customer_at,
    o.estimated_delivery_date,
    coalesce(i.item_count, 0)                   as item_count,
    coalesce(i.seller_count, 0)                 as seller_count,
    coalesce(i.items_value, 0)                  as items_value,
    coalesce(i.freight_value, 0)                as freight_value,
    coalesce(i.items_value + i.freight_value, 0) as order_value,
    coalesce(p.payment_value, 0)                as payment_value,
    coalesce(p.payment_count, 0)                as payment_count,
    p.max_installments,
    p.primary_payment_type,
    coalesce(p.used_voucher, false)             as used_voucher,
    d.purchase_to_approval_days,
    d.purchase_to_carrier_days,
    d.delivery_lead_days,
    d.estimated_lead_days,
    d.days_vs_estimate,
    d.is_delivered,
    d.is_late,
    r.review_count,
    r.avg_review_score
from {{ ref('stg_orders') }} as o
join {{ ref('stg_customers') }} as c using (customer_id)
left join items as i using (order_id)
left join {{ ref('int_order_payments') }} as p using (order_id)
left join {{ ref('int_order_reviews') }} as r using (order_id)
join {{ ref('int_order_delivery') }} as d using (order_id)
