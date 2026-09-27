{{ config(
    severity='error', error_if='>500', warn_if='>0',
    meta={'dq_severity': 'ERROR', 'tolerance': 500, 'model': 'fct_orders'}
) }}
{#
  ERROR with tolerance: non-canceled orders without vouchers whose payments fall short of the
  order value by more than R$1.00. Over-payment is NOT flagged (consistent with instalment
  interest). Measured on Olist v2: 18 of 94,454 eligible orders. Tolerance 500 (~0.5%)
  absorbs that level of source noise; a transformation bug that breaks the payment join
  would exceed it immediately and block publication.
#}
select order_id, order_value, payment_value, payment_value - order_value as difference
from {{ ref('fct_orders') }}
where not is_canceled
  and not used_voucher
  and item_count > 0
  and payment_value < order_value - 1.00
