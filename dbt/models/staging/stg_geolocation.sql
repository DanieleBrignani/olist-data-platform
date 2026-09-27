select
    geolocation_zip_code_prefix                 as zip_code_prefix,
    geolocation_lat                             as lat,
    geolocation_lng                             as lng,
    {{ normalize_city('geolocation_city') }}    as city,
    geolocation_state                           as state,
    source_record_count,
    -- same bounding box as the contract rule coordinates_within_brazil (WARNING)
    geolocation_lat between -34.0 and 5.5
        and geolocation_lng between -74.0 and -28.0 as is_within_brazil
from {{ source('src', 'geolocation') }}
