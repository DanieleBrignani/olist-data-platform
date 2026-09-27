select
    s.seller_id,
    s.zip_code_prefix,
    s.city,
    s.state,
    p.first_sale_at::date   as first_sale_date
from {{ ref('stg_sellers') }} as s
left join {{ ref('int_seller_performance') }} as p using (seller_id)
