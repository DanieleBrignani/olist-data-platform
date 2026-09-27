select
    i.order_id,
    i.order_item_id,
    i.product_id,
    i.seller_id,
    o.customer_unique_id,
    {{ date_key('i.purchased_at') }}        as purchase_date_key,
    {{ date_key('i.shipping_limit_at') }}   as shipping_limit_date_key,
    o.is_canceled,
    i.shipped_after_limit,
    i.price,
    i.freight_value,
    i.item_total
from {{ ref('int_order_items_enriched') }} as i
join {{ ref('int_order_enriched') }} as o using (order_id)
