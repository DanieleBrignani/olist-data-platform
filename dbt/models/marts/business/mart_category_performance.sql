-- Grain: one row per product category (English name, Portuguese fallback, 'uncategorized').
-- Order-level outcomes (review, lateness) are attributed to every category in the order.
with category_orders as (
    select distinct p.category_name, i.order_id
    from {{ ref('fct_order_items') }} as i
    join {{ ref('dim_product') }} as p using (product_id)
    where not i.is_canceled
)

select
    p.category_name,
    count(distinct i.order_id)                                  as orders,
    count(*)                                                    as items_sold,
    count(distinct i.product_id)                                as distinct_products,
    count(distinct i.seller_id)                                 as distinct_sellers,
    sum(i.price)                                                as revenue,
    round(avg(i.price), 2)                                      as avg_item_price,
    sum(i.freight_value)                                        as freight_value,
    round(sum(i.freight_value) / nullif(sum(i.price), 0), 4)    as freight_to_price_ratio,
    max(o.avg_review_score)                                     as avg_review_score,
    max(o.late_rate)                                            as late_rate
from {{ ref('fct_order_items') }} as i
join {{ ref('dim_product') }} as p using (product_id)
join (
    select
        co.category_name,
        round(avg(f.avg_review_score), 2)                                           as avg_review_score,
        round(count(*) filter (where f.is_late)::numeric
              / nullif(count(*) filter (where f.is_delivered), 0), 4)               as late_rate
    from category_orders as co
    join {{ ref('fct_orders') }} as f using (order_id)
    group by co.category_name
) as o using (category_name)
where not i.is_canceled
group by p.category_name
