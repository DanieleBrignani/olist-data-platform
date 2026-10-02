# ADR-0011: Stable source identifiers as dimension keys; no SCD2

**Status:** Accepted (2026-09-27)

## Context
Kimball-style warehouses usually give dimensions integer surrogate keys and track attribute
history with SCD type 2. The Olist source is a single snapshot:
* it has no change timestamps and no history of addresses or categories;
* its identifiers are already opaque, globally unique 32-character hex strings that are stable
  across the dataset.

## Decision
* Dimensions are keyed by the source identifier (`customer_unique_id`, `product_id`,
  `seller_id`, `zip_code_prefix`). `dim_date` uses an integer `yyyymmdd` key.
* All dimensions are SCD type 1: they hold the current value only.
* History that genuinely exists *per event* stays on the fact. For example,
  `fct_orders.zip_code_prefix` is the delivery location of that specific order, while
  `dim_customer.zip_code_prefix` is the person's latest one.

## Consequences
+ Keys are stable across full rebuilds and dataset versions, with no key re-mapping, so
  downstream extracts can join on them safely.
+ There is no fabricated history. An SCD2 table built from one snapshot would have exactly one
  version per row and would imply a tracking capability the data cannot support.
− Text keys are wider than integers. At about 100k rows per fact the join cost is small; the
  The staging and benchmark measurements are where this would show up.
− If the source ever becomes a change feed, SCD2 on `dim_customer` (address) and
  `dim_product` (category) would be the first additions. This ADR would then be superseded.
