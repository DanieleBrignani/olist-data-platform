-- One row per CEP zip prefix seen ANYWHERE (geolocation, customers, sellers), so every
-- customer/seller location resolves to a geography row even when the geolocation file has
-- no coordinates for it (278 customers, 7 sellers on Olist v2).
with points as (
    select zip_code_prefix, lat, lng
    from {{ ref('stg_geolocation') }}
    where is_within_brazil          -- mis-geocoded points are excluded from the centroid
),

coordinates as (
    select
        zip_code_prefix,
        round(avg(lat), 6)  as latitude,
        round(avg(lng), 6)  as longitude,
        count(*)            as coordinate_points
    from points
    group by zip_code_prefix
),

-- Most frequent (city, state) per prefix across all sources, weighted by record count
names as (
    select zip_code_prefix, city, state, sum(weight) as weight
    from (
        select zip_code_prefix, city, state, source_record_count as weight
        from {{ ref('stg_geolocation') }}
        union all
        select zip_code_prefix, city, state, 1 from {{ ref('stg_customers') }}
        union all
        select zip_code_prefix, city, state, 1 from {{ ref('stg_sellers') }}
    ) as all_names
    group by zip_code_prefix, city, state
),

ranked as (
    select
        *,
        row_number() over (
            partition by zip_code_prefix order by weight desc, city, state
        ) as rank_in_prefix
    from names
)

select
    r.zip_code_prefix,
    r.city,
    r.state,
    c.latitude,
    c.longitude,
    coalesce(c.coordinate_points, 0)    as coordinate_points,
    c.zip_code_prefix is not null       as has_coordinates
from ranked as r
left join coordinates as c using (zip_code_prefix)
where r.rank_in_prefix = 1
