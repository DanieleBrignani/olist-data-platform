{{ config(severity='warn', meta={'dq_severity': 'WARNING', 'model': 'fct_reviews'}) }}
{#
  WARNING cross-file invariant: a satisfaction survey answered before the order was placed.
  Measured on Olist v2: 60 reviews at date grain (63 comparing full timestamps; the
  answer-date key is day-grained). Real source anomaly (probably a review linked to the
  wrong order), so it is reported rather than allowed to block publication.
#}
select r.review_id, r.order_id, o.purchased_at, ad.date_day as answered_date
from {{ ref('fct_reviews') }} as r
join {{ ref('fct_orders') }} as o using (order_id)
join {{ ref('dim_date') }} as ad on ad.date_key = r.review_answered_date_key
where ad.date_day < o.purchased_at::date
