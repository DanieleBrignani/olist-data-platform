-- Grain: one row per month the survey was sent x review score (1..5).
select
    d.month_start,
    to_char(d.month_start, 'YYYY-MM')                                       as year_month,
    r.review_score,
    count(*)                                                                as reviews,
    round(count(*)::numeric / sum(count(*)) over (partition by d.month_start), 4)
                                                                            as share_of_month,
    count(*) filter (where r.has_comment)                                   as reviews_with_comment,
    count(*) filter (where r.order_was_late)                                as reviews_of_late_orders
from {{ ref('fct_reviews') }} as r
join {{ ref('dim_date') }} as d on d.date_key = r.review_sent_date_key
group by d.month_start, r.review_score
