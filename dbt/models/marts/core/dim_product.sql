select
    p.product_id,
    coalesce(t.category_name_en, p.category_name_pt, 'uncategorized') as category_name,
    p.category_name_pt,
    t.category_name_en is not null                          as has_english_category,
    p.name_length,
    p.description_length,
    p.photos_qty,
    p.weight_g,
    p.length_cm,
    p.height_cm,
    p.width_cm,
    p.length_cm * p.height_cm * p.width_cm                  as volume_cm3
from {{ ref('stg_products') }} as p
left join {{ ref('stg_category_translation') }} as t using (category_name_pt)
