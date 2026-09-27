# Database design: raw → src

Measured numbers live in generated files. This document explains the decisions behind them:
* [query_plans.md](query_plans.md): `EXPLAIN ANALYZE` with and without each index
  (`scripts/explain_queries.py`).
* [source_profile.md](source_profile.md): data facts behind every constraint.

## Layers owned by migrations

| Schema | Content | Constraints | Rebuilt by |
|--------|---------|-------------|------------|
| `meta` | runs, files, events, record issues | PK, FK to runs/files, CHECKs on statuses and severities, `UNIQUE (source_table, sha256)` | append-only |
| `raw` | 1:1 text copy of each file + lineage | PK `(_source_file_id, _row_number)`, FK to `meta.source_files` | `ingest_raw`, per file |
| `src` | typed, contract-conforming rows | PK/FK/UNIQUE/CHECK below | `load_staging`, all tables in one transaction |

## src constraints

The staging engine already quarantines violating records **before** they are inserted, so
under normal operation no constraint ever fires. The constraints exist to catch bugs in the
engine: if one does fire, the whole `load_staging` transaction fails loudly instead of
publishing wrong data.

| Kind | Where | Why |
|------|-------|-----|
| Primary keys | every table except geolocation; composite keys for items `(order_id, order_item_id)`, payments `(order_id, payment_sequential)`, reviews `(review_id, order_id)` | grain of each table, from the contracts (reviews: `review_id` alone is not unique in the source) |
| Unique | `orders.customer_id`; geolocation natural key over all 5 columns | 1:1 order↔customer key; geolocation rows are distinct points after collapsing duplicates |
| Foreign keys | orders→customers, items→orders/products/sellers, payments→orders, reviews→orders | only relationships the contracts classify as ERROR. The zip→geolocation and category→translation links are WARNING existence checks, because the source genuinely has gaps (278 customers, 7 sellers, 13 products) |
| CHECK | ids `^[0-9a-f]{32}$`, zips `^[0-9]{5}$`, UF `^[A-Z]{2}$`, `price > 0`, `freight_value >= 0`, `payment_value >= 0`, `review_score 1..5`, positive dimensions, lat/lng ranges, `delivered >= purchased`, accepted `order_status` | the invariants downstream metrics depend on |
| Lineage | `_source_file_id`, `_row_number`, `source_record_count` on every row | any src row can be traced to its raw record; collapsed duplicates stay countable |

Type choices:
* **`numeric` without scale for money and coordinates.** Casting to `numeric(12,2)` would
  silently round unexpected extra decimals. The warehouse can pick a presentation scale later.
* **`timestamp` without time zone.** The source carries no zone. The values are assumed to be
  Brazilian local time, and that assumption is documented in the contracts.
* **Text for zip prefixes,** to keep the leading zeros.

## Staging engine (set-based, one transaction)

Execution order per table follows the foreign keys, parents first:

1. **Text checks on raw:** not-null, type (`pg_input_is_valid`, PostgreSQL 16+), pattern,
   max length.
2. **Typed stage:** built from records with no quarantined issue; an empty string becomes
   NULL for every column.
3. **Contract rules:** evaluated on typed values; a NULL never violates a rule.
4. **Exact duplicates:** flagged, unless the contract expects them (geolocation).
5. **Primary-key conflicts:** same key, different content → every version is quarantined.
6. **Foreign keys:** checked against the parent's *valid* rows, so a quarantined order
   cascades to its items, payments and reviews.
7. **Collapse:** exact duplicates become one row with its `source_record_count`.

Reconciliation invariant, asserted by tests on synthetic and real data:
`raw records = sum(src.source_record_count) + distinct quarantined records`.

**Performance lesson (measured):** the first version expanded every record into one
candidate row per check (`CROSS JOIN LATERAL (VALUES …)`), which meant 10 M rows for
geolocation, and computed a reason string for each. A boolean prefilter (`any check
violated?`) now runs first, so the expansion only happens for violating records. Geolocation
staging went from 15.8 s to 2.9 s with identical results.

## Index decisions

Every index must earn its place with a measured plan. See [query_plans.md](query_plans.md)
for the numbers.

| Index | Query pattern | Verdict |
|-------|---------------|---------|
| `ix_src_order_items_seller_id` | one seller's items (seller performance drill-down) | keep: the plan switches from a full scan of `order_items` to a bitmap index scan |
| `ix_src_orders_purchase_ts` | a narrow purchase-date window | keep: a bitmap index scan replaces the full scan |
| `ix_src_order_reviews_order_id` | reviews of one order (FK lookup) | keep: the largest measured gain; without it every lookup is a full scan |
| `ix_src_orders_purchase_ts` for **full-history aggregation** (Q3) | monthly revenue over all orders | **the index does not help**: the planner correctly scans every row either way. The bottleneck is the hash join plus a sort for `GROUP BY` that spills to disk under the default `work_mem`. The remedy is pre-aggregation in the marts (Phase 6), not another index |
| `ix_src_order_items_product_id`, `ix_src_geolocation_zip` | FK / existence-check joins | kept for join paths. **Not individually re-measured on the warehouse**: the only warehouse index with a measured query is `fct_order_items(seller_id)` (benchmark Q5, sub-millisecond) |

Indexes that were deliberately **not** created:
* **Low-cardinality columns** (`order_status`, `payment_type`, UF codes): a filter on them
  selects a large share of the table, so a sequential scan wins.
* **Indexes that duplicate a PK prefix:** `order_items(order_id)` and
  `order_payments(order_id)` are already covered by their composite primary keys.
