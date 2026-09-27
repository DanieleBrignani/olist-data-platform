-- Grain: one row per purchase month x customer state, delivered orders only.
-- Late = delivered on a later calendar day than the estimated delivery date.
select
    d.month_start,
    to_char(d.month_start, 'YYYY-MM')                                           as year_month,
    g.state                                                                     as customer_state,
    count(*)                                                                    as delivered_orders,
    round(avg(f.delivery_lead_days), 2)                                         as avg_delivery_days,
    round((percentile_cont(0.5) within group (order by f.delivery_lead_days))::numeric, 2)
                                                                                as median_delivery_days,
    round((percentile_cont(0.9) within group (order by f.delivery_lead_days))::numeric, 2)
                                                                                as p90_delivery_days,
    round(avg(f.estimated_lead_days), 2)                                        as avg_estimated_days,
    count(*) filter (where f.is_late)                                           as late_orders,
    round(count(*) filter (where f.is_late)::numeric / count(*), 4)             as late_rate,
    round(avg(f.days_vs_estimate) filter (where f.is_late), 2)                  as avg_days_late_when_late
from {{ ref('fct_orders') }} as f
join {{ ref('dim_date') }} as d on d.date_key = f.purchase_date_key
join {{ ref('dim_geography') }} as g on g.zip_code_prefix = f.zip_code_prefix
where f.is_delivered
group by d.month_start, g.state
