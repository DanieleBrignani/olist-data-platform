select
    order_id,
    payment_sequential,
    payment_type,
    payment_installments,
    payment_value
from {{ source('src', 'order_payments') }}
