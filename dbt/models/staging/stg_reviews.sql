select
    review_id,
    order_id,
    review_score,
    review_comment_title                                            as comment_title,
    review_comment_message                                          as comment_message,
    coalesce(trim(review_comment_message), '') <> ''                as has_comment,
    review_creation_date                                            as review_sent_at,
    review_answer_timestamp                                         as review_answered_at
from {{ source('src', 'order_reviews') }}
