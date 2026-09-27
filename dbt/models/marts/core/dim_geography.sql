select
    zip_code_prefix,
    city,
    state,
    latitude,
    longitude,
    coordinate_points,
    has_coordinates
from {{ ref('int_geography') }}
