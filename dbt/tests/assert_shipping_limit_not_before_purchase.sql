{{ config(meta={'dq_severity': 'CRITICAL', 'model': 'fct_order_items'}) }}
{#
  CRITICAL cross-file invariant: the seller's shipping deadline cannot precede the purchase.
  No single-file contract can check it - the items file has no purchase date - so this is
  where it is enforced. Measured on Olist v2: 0 violations.
#}
select
    i.order_id,
    i.order_item_id,
    o.purchased_at,
    sl.date_day as shipping_limit_date
from {{ ref('fct_order_items') }} as i
join {{ ref('fct_orders') }} as o using (order_id)
join {{ ref('dim_date') }} as sl on sl.date_key = i.shipping_limit_date_key
where sl.date_day < o.purchased_at::date
