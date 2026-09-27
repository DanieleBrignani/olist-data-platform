-- One row per PERSON (customer_unique_id). Olist issues a new customer_id per order, so the
-- person-level view only exists after grouping; the "current" address is the one used on the
-- person's most recent order (SCD type 1: the snapshot has no address history).
with ranked as (
    select
        *,
        row_number() over (
            partition by customer_unique_id order by purchased_at desc, order_id
        ) as recency_rank
    from {{ ref('int_order_enriched') }}
)

select
    customer_unique_id,
    max(customer_zip_code_prefix) filter (where recency_rank = 1)  as zip_code_prefix,
    max(customer_state) filter (where recency_rank = 1)            as state,
    min(purchased_at)                                              as first_order_at,
    max(purchased_at)                                              as last_order_at,
    count(*)                                                       as order_count,
    count(*) filter (where not is_canceled)                        as valid_order_count,
    sum(order_value) filter (where not is_canceled)                as lifetime_value,
    round(avg(avg_review_score), 2)                                as avg_review_score
from ranked
group by customer_unique_id
