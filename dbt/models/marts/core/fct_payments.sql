select
    p.order_id,
    p.payment_sequential,
    o.customer_unique_id,
    {{ date_key('o.purchased_at') }}    as purchase_date_key,
    p.payment_type,
    o.is_canceled,
    p.payment_installments,
    p.payment_value
from {{ ref('stg_payments') }} as p
join {{ ref('int_order_enriched') }} as o using (order_id)
