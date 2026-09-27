select
    r.review_id,
    r.order_id,
    o.customer_unique_id,
    {{ date_key('r.review_sent_at') }}                          as review_sent_date_key,
    {{ date_key('r.review_answered_at') }}                      as review_answered_date_key,
    r.review_score,
    r.has_comment,
    r.comment_title is not null                                 as has_title,
    o.is_late                                                   as order_was_late,
    {{ days_between('r.review_sent_at', 'r.review_answered_at') }} as response_days
from {{ ref('stg_reviews') }} as r
join {{ ref('int_order_enriched') }} as o using (order_id)
