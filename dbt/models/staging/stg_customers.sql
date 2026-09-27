select
    customer_id,
    customer_unique_id,
    customer_zip_code_prefix                    as zip_code_prefix,
    {{ normalize_city('customer_city') }}       as city,
    customer_state                              as state
from {{ source('src', 'customers') }}
