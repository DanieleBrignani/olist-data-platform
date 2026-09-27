-- Delivery timeline of every order. Durations are NULL when a milestone is missing.
select
    order_id,
    {{ days_between('purchased_at', 'approved_at') }}                as purchase_to_approval_days,
    {{ days_between('purchased_at', 'delivered_to_carrier_at') }}    as purchase_to_carrier_days,
    {{ days_between('purchased_at', 'delivered_to_customer_at') }}   as delivery_lead_days,
    (estimated_delivery_date - purchased_at::date)                   as estimated_lead_days,
    order_status = 'delivered' and delivered_to_customer_at is not null as is_delivered,
    case
        when delivered_to_customer_at is null then null
        else delivered_to_customer_at::date - estimated_delivery_date
    end                                                              as days_vs_estimate,
    case
        when delivered_to_customer_at is null then null
        else delivered_to_customer_at::date
             > estimated_delivery_date + {{ var('late_delivery_grace_days') }}
    end                                                              as is_late
from {{ ref('stg_orders') }}
