-- One row per order that received at least one review (an order can have several)
select
    order_id,
    count(*)                        as review_count,
    round(avg(review_score), 2)     as avg_review_score,
    min(review_score)               as min_review_score
from {{ ref('stg_reviews') }}
group by order_id
