-- Grain: one row per purchase month x payment type.
select
    d.month_start,
    to_char(d.month_start, 'YYYY-MM')           as year_month,
    p.payment_type,
    count(*)                                    as payments,
    count(distinct p.order_id)                  as orders,
    sum(p.payment_value)                        as payment_volume,
    round(avg(p.payment_value), 2)              as avg_payment_value,
    round(avg(p.payment_installments), 2)       as avg_installments
from {{ ref('fct_payments') }} as p
join {{ ref('dim_date') }} as d on d.date_key = p.purchase_date_key
group by d.month_start, p.payment_type
