-- One row per order that has at least one payment record
with ranked as (
    select
        *,
        row_number() over (
            partition by order_id order by payment_value desc, payment_sequential
        ) as value_rank
    from {{ ref('stg_payments') }}
)

select
    order_id,
    sum(payment_value)                                          as payment_value,
    count(*)                                                    as payment_count,
    max(payment_installments)                                   as max_installments,
    -- the instrument that paid the largest share of the order
    max(payment_type) filter (where value_rank = 1)             as primary_payment_type,
    count(*) filter (where payment_type = 'voucher') > 0        as used_voucher
from ranked
group by order_id
