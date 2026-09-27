select
    customer_unique_id,
    zip_code_prefix,
    state,
    first_order_at::date                as first_order_date,
    valid_order_count > 1               as is_repeat_customer
from {{ ref('int_customer_orders') }}
