select
    i.order_id,
    i.order_item_id,
    i.product_id,
    i.seller_id,
    i.shipping_limit_at,
    i.price,
    i.freight_value,
    i.price + i.freight_value                                   as item_total,
    coalesce(t.category_name_en, p.category_name_pt, 'uncategorized') as category_name,
    o.purchased_at,
    o.delivered_to_carrier_at,
    -- seller handed over after the deadline the marketplace gave them
    o.delivered_to_carrier_at > i.shipping_limit_at             as shipped_after_limit
from {{ ref('stg_order_items') }} as i
join {{ ref('stg_orders') }} as o using (order_id)
join {{ ref('stg_products') }} as p using (product_id)
left join {{ ref('stg_category_translation') }} as t using (category_name_pt)
