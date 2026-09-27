# Data model

Star schema built with dbt in `warehouse_build`, published as `warehouse` after the quality gate.
Column-level documentation lives in `dbt/models/marts/core/_core.yml`. Those contracts are
**enforced**: PostgreSQL holds 9 primary keys and 20 foreign keys, and dbt fails the build if a
column or type drifts.

```
                    dim_date ─────────────┬──────────────┬──────────────┐
                        │                 │              │              │
 dim_geography ── dim_customer ──── fct_orders ◄── fct_order_items ── dim_product
       │                │                 ▲              │
       └──── dim_seller ┼─────────────────┼──────────────┘
                        ├──── fct_payments (→ fct_orders)
                        └──── fct_reviews  (→ fct_orders)
```

## Layers

| Layer | Schema | Materialisation | Purpose |
|-------|--------|-----------------|---------|
| staging | `stg` | view | 1:1 with `src`. Renames to business names and normalises city text. No joins. |
| intermediate | `int` | view | Reusable logic: per-order delivery timeline, payments and reviews; per-person history; per-seller performance; per-zip geography resolution. |
| core | `warehouse_build` → `warehouse` | table, enforced contract | Dimensions and facts. |
| business | `marts_build` → `marts` | table | Pre-aggregated answers for reporting. |

No model is incremental (ADR-0006): every run is a full, deterministic rebuild of a static
snapshot, and a full `dbt build` takes seconds at this volume.

## Facts

| Fact | Business process | Grain | Primary key | Foreign keys | Additive measures | Non-additive measures |
|------|------------------|-------|-------------|--------------|-------------------|-----------------------|
| `fct_orders` | order placement and fulfilment | one order | `order_id` | `customer_unique_id`→dim_customer, `zip_code_prefix`→dim_geography, `purchase_date_key` / `delivered_date_key` / `estimated_delivery_date_key`→dim_date | item_count, items_value, freight_value, order_value, payment_value, payment_count, review_count, flags counted as 0/1 | max_installments, purchase_to_approval/carrier_days, delivery_lead_days, estimated_lead_days, days_vs_estimate, avg_review_score, seller_count |
| `fct_order_items` | sale of a product by a seller | one order line (`order_id`, `order_item_id`) | composite | `order_id`→fct_orders, `product_id`→dim_product, `seller_id`→dim_seller, `customer_unique_id`→dim_customer, `purchase_date_key` / `shipping_limit_date_key`→dim_date | price, freight_value, item_total, row count = quantity | – |
| `fct_payments` | payment of an order | one payment instrument within an order (`order_id`, `payment_sequential`) | composite | `order_id`→fct_orders, `customer_unique_id`→dim_customer, `purchase_date_key`→dim_date | payment_value | payment_installments |
| `fct_reviews` | post-purchase satisfaction survey | one (`review_id`, `order_id`); `review_id` alone is not unique in the source | composite | `order_id`→fct_orders, `customer_unique_id`→dim_customer, `review_sent_date_key` / `review_answered_date_key`→dim_date | counts only | review_score (average, never sum), response_days |

`fct_order_items`, `fct_payments` and `fct_reviews` reference `fct_orders` by `order_id`: the
order is a degenerate header shared by three processes at different grains. Keeping them as
separate facts avoids the classic fan-out error. Joining payments to items would multiply
payment values by the number of items.

## Dimensions and why each exists

| Dimension | Grain | Why it exists (instead of attributes on the facts) |
|-----------|-------|----------------------------------------------------|
| `dim_date` | calendar day | Every analysis is by day, month or weekday, and the source only has timestamps. It covers every date any fact references, including the 2020 shipping limits, so every date key resolves. |
| `dim_customer` | **person** (`customer_unique_id`) | The source's `customer_id` is issued *per order*, so the raw "customers" file really holds order addresses. Repeat behaviour and lifetime value only exist at person level: 99,441 `customer_id`s collapse to 96,096 people. The current address is the latest order's (SCD1); each order's own delivery location stays on `fct_orders.zip_code_prefix`. |
| `dim_product` | product | Category reporting needs the English category, falling back to the Portuguese name (2 untranslated categories) and then to `uncategorized` (products with no category). Physical attributes support freight analysis. |
| `dim_seller` | seller | Seller performance (lateness, reviews, revenue) is a primary operational question, and sellers have their own location. |
| `dim_geography` | CEP zip prefix | Customers and sellers share locations. The geolocation file has many points per prefix, inconsistent city spellings and mis-geocoded points. This dimension resolves each prefix to one city/state and the centroid of its in-Brazil points. It includes prefixes missing from the geolocation file (`has_coordinates = false`), so every foreign key resolves. |

Rejected as dimensions:
* **Order status and payment type:** kept as degenerate attributes on the facts. They are
  single low-cardinality codes with no further attributes, so a dimension would add a join
  and no information.
* **Review text:** not a dimension; it is free text attached to one review row.

## Metric definitions (used by the marts and pinned by tests)

| Metric | Definition |
|--------|------------|
| GMV | Σ(item price + freight) of orders **not** canceled/unavailable |
| AOV | GMV / number of non-canceled orders **with at least one item** |
| Payment volume | Σ payments of all orders. It can differ from GMV: vouchers, instalment interest, payments on canceled orders |
| Cancellation rate | orders with status canceled or unavailable / all orders |
| Delivery lead time | purchase → delivery to customer, days (fractional) |
| Late delivery | delivered on a later **calendar day** than the estimated delivery date (the estimate is a date) |
| Repeat customer | a person with more than one non-canceled order |
| Seller shipped after limit | carrier hand-off later than the seller's `shipping_limit_date` |

Each definition is asserted on a hand-computable synthetic scenario in
`tests/integration/test_dbt_models.py` (for example GMV 144.80 and AOV 72.40 for a month with
one canceled order and one order without items).

## Known data properties reflected in the model

* **The last months of the snapshot are nearly empty and mostly canceled.** Measured in
  `mart_sales`: 2018-09 has 16 orders (15 canceled) and 2018-10 has 4 (all canceled), against
  about 6,500 a month before. This is a property of the extract, not a modelling error. It is
  surfaced by a WARNING test (Phase 7) and called out in business reporting.
* **775 orders have no items** (canceled/unavailable at creation). They are kept in
  `fct_orders` with zero value, counted as orders and excluded from AOV.
* **166 zip prefixes have no usable coordinates.** They are kept in `dim_geography` with
  `has_coordinates = false`.
