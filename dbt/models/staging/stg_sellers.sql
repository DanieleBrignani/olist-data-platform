select
    seller_id,
    seller_zip_code_prefix                  as zip_code_prefix,
    {{ normalize_city('seller_city') }}     as city,
    seller_state                            as state
from {{ source('src', 'sellers') }}
