-- Calendar covering every date referenced by any fact (purchase, delivery, estimate,
-- shipping limit, review), so every date key resolves.
with bounds as (
    select min(d)::date as start_date, max(d)::date as end_date
    from (
        select purchased_at as d from {{ ref('stg_orders') }}
        union all select delivered_to_customer_at from {{ ref('stg_orders') }}
        union all select estimated_delivery_date::timestamp from {{ ref('stg_orders') }}
        union all select shipping_limit_at from {{ ref('stg_order_items') }}
        union all select review_sent_at from {{ ref('stg_reviews') }}
        union all select review_answered_at from {{ ref('stg_reviews') }}
    ) as all_dates
),

days as (
    select generate_series(start_date, end_date, interval '1 day')::date as date_day
    from bounds
)

select
    {{ date_key('date_day') }}                      as date_key,
    date_day,
    extract(isoyear from date_day)::integer         as iso_year,
    extract(year from date_day)::integer            as year,
    extract(quarter from date_day)::integer         as quarter,
    extract(month from date_day)::integer           as month,
    to_char(date_day, 'FMMonth')                    as month_name,
    to_char(date_day, 'YYYY-MM')                    as year_month,
    date_trunc('month', date_day)::date             as month_start,
    extract(week from date_day)::integer            as iso_week,
    extract(day from date_day)::integer             as day_of_month,
    extract(isodow from date_day)::integer          as day_of_week,
    to_char(date_day, 'FMDay')                      as day_name,
    extract(isodow from date_day) in (6, 7)         as is_weekend
from days
