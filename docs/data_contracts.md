# Source data contracts

Generated from `data_contracts/*.yml` by `olist contracts docs`. Do not edit by hand.

Implicit rules for every contract: values must cast to the declared type, non-nullable
columns must be present, `pattern`/`max length` must hold, primary keys must be unique.
Violations are ERROR (record quarantined in `meta.rejected_records`). If the share of
rejected records exceeds the contract's max reject ratio the file is CRITICAL.

| Table | File | Grain | Columns | Rules |
|-------|------|-------|--------:|------:|
| `customers` | `olist_customers_dataset.csv` | One record per order-level customer key (customer_id). | 5 | 1 |
| `geolocation` | `olist_geolocation_dataset.csv` | One record per observed (zip prefix, lat, lng, city, state) point. Exact duplicate records are collapsed in src (counted and logged as WARNING, kept in raw for lineage). | 5 | 4 |
| `order_items` | `olist_order_items_dataset.csv` | One record per item line within an order (order_id, order_item_id). | 7 | 4 |
| `order_payments` | `olist_order_payments_dataset.csv` | One record per payment instrument within an order (order_id, payment_sequential). | 5 | 6 |
| `order_reviews` | `olist_order_reviews_dataset.csv` | One record per (review_id, order_id). review_id alone is NOT unique in the source: the same survey response can be linked to several orders (measured in docs/source_profile.md). | 7 | 3 |
| `orders` | `olist_orders_dataset.csv` | One record per order (order_id). | 8 | 8 |
| `product_category_translation` | `product_category_name_translation.csv` | One record per Portuguese category name. | 2 | 0 |
| `products` | `olist_products_dataset.csv` | One record per product (product_id). | 9 | 4 |
| `sellers` | `olist_sellers_dataset.csv` | One record per seller (seller_id). | 4 | 2 |

## `customers` ← `olist_customers_dataset.csv`

Customer record attached to each order, with the delivery location (zip prefix, city, state). Olist creates one customer_id per order; customer_unique_id identifies the person.

* **Grain:** One record per order-level customer key (customer_id).
* **Primary key:** `customer_id`
* **Format:** utf-8, LF, delimiter `,`
* **Max reject ratio before CRITICAL:** 1.00%

| Column | Type | Nullable | Unique | Constraint | Meaning |
|--------|------|----------|--------|------------|---------|
| `customer_id` | text | no | yes | pattern `^[0-9a-f]{32}$`, max len 32 | Order-scoped customer key; referenced by orders.customer_id. Primary key. |
| `customer_unique_id` | text | no |  | pattern `^[0-9a-f]{32}$`, max len 32 | Stable identifier of the person across orders. NOT unique in this file (repeat buyers); it is the natural key of dim_customer. |
| `customer_zip_code_prefix` | text | no |  | pattern `^[0-9]{5}$`, max len 5 | First 5 digits of the Brazilian CEP. Stored as text because leading zeros are significant (e.g. 01003 in São Paulo). |
| `customer_city` | text | no |  |  | Delivery city name, lower-case, without accents in most records. |
| `customer_state` | text | no |  | max len 2 | Two-letter Brazilian federative unit (UF) code. |

| Foreign key | References | Severity | Why |
|---|---|---|---|
| customer_zip_code_prefix | geolocation(geolocation_zip_code_prefix) | WARNING | Existence check only (geolocation has many rows per prefix). Some prefixes are absent from the geolocation file; such customers keep their city/state but get no coordinates. |

| Rule | Check | Severity | Why |
|---|---|---|---|
| `customer_state_is_uf` | `customer_state` in {AC, AL, AM, AP, BA, CE, DF, ES, GO, MA, MG, MS, MT, PA, PB, PE, PI, PR, RJ, RN, RO, RR, RS, SC, SE, SP, TO} | ERROR | State must be one of the 27 Brazilian federative units. |

## `geolocation` ← `olist_geolocation_dataset.csv`

Reference list of coordinates observed for each CEP zip prefix. Not an entity table: a prefix has many coordinate points, and the file contains many exact duplicate records.

* **Grain:** One record per observed (zip prefix, lat, lng, city, state) point. Exact duplicate records are collapsed in src (counted and logged as WARNING, kept in raw for lineage).
* **Primary key:** — (none; see grain)
* **Format:** utf-8, LF, delimiter `,`
* **Max reject ratio before CRITICAL:** 1.00%
* **Exact duplicate records:** collapsed in `src`, logged as WARNING

| Column | Type | Nullable | Unique | Constraint | Meaning |
|--------|------|----------|--------|------------|---------|
| `geolocation_zip_code_prefix` | text | no |  | pattern `^[0-9]{5}$`, max len 5 | First 5 digits of the CEP; text to preserve leading zeros. |
| `geolocation_lat` | numeric | no |  |  | Latitude in decimal degrees (WGS84 assumed; not stated by the source). |
| `geolocation_lng` | numeric | no |  |  | Longitude in decimal degrees. |
| `geolocation_city` | text | no |  |  | City name as recorded, with inconsistent accents/casing across records. |
| `geolocation_state` | text | no |  | max len 2 | Two-letter Brazilian federative unit (UF) code. |

| Rule | Check | Severity | Why |
|---|---|---|---|
| `latitude_valid` | -90 ≤ `geolocation_lat` ≤ 90 | ERROR | Latitude outside [-90, 90] is not a coordinate. |
| `longitude_valid` | -180 ≤ `geolocation_lng` ≤ 180 | ERROR | Longitude outside [-180, 180] is not a coordinate. |
| `coordinates_within_brazil` | `geolocation_lat BETWEEN -34.0 AND 5.5 AND geolocation_lng BETWEEN -74.0 AND -28.0` | WARNING | Points outside Brazil's bounding box are mis-geocoded; kept but excluded when computing the representative coordinate of a prefix in dim_geography. |
| `geolocation_state_is_uf` | `geolocation_state` in {AC, AL, AM, AP, BA, CE, DF, ES, GO, MA, MG, MS, MT, PA, PB, PE, PI, PR, RJ, RN, RO, RR, RS, SC, SE, SP, TO} | ERROR | State must be one of the 27 Brazilian federative units. |

## `order_items` ← `olist_order_items_dataset.csv`

Line items of each order: which product was sold by which seller, at what price and freight. Quantity is represented by repeated lines (order_item_id 1..n), not a column.

* **Grain:** One record per item line within an order (order_id, order_item_id).
* **Primary key:** `order_id`, `order_item_id`
* **Format:** utf-8, LF, delimiter `,`
* **Max reject ratio before CRITICAL:** 1.00%

| Column | Type | Nullable | Unique | Constraint | Meaning |
|--------|------|----------|--------|------------|---------|
| `order_id` | text | no |  | pattern `^[0-9a-f]{32}$`, max len 32 | Parent order. Part of the primary key. |
| `order_item_id` | integer | no |  |  | Sequential line number inside the order, starting at 1. Part of the primary key. |
| `product_id` | text | no |  | pattern `^[0-9a-f]{32}$`, max len 32 | Product sold on this line. |
| `seller_id` | text | no |  | pattern `^[0-9a-f]{32}$`, max len 32 | Seller who fulfils this line (an order can have several sellers). |
| `shipping_limit_date` | timestamp | no |  |  | Deadline for the seller to hand the item to the logistics partner. |
| `price` | numeric | no |  |  | Item price in BRL, excluding freight. |
| `freight_value` | numeric | no |  |  | Freight charged for this item in BRL. When an order has several items the order freight is split across them. |

| Foreign key | References | Severity | Why |
|---|---|---|---|
| order_id | orders(order_id) | ERROR | An item without its order cannot be attributed to a customer or date. |
| product_id | products(product_id) | ERROR | An item must reference a known product for category reporting. |
| seller_id | sellers(seller_id) | ERROR | An item must reference a known seller for seller performance. |

| Rule | Check | Severity | Why |
|---|---|---|---|
| `order_item_id_positive` | 1 ≤ `order_item_id` | ERROR | Line numbers start at 1. |
| `price_positive` | `price > 0` | ERROR | A zero or negative item price is not a sale. |
| `freight_non_negative` | 0 ≤ `freight_value` | ERROR | Freight can be free (0) but never negative. |
| `shipping_limit_not_in_future` | `shipping_limit_date <= LOCALTIMESTAMP` | ERROR | A deadline in the future relative to load time is invalid for a closed snapshot. |

## `order_payments` ← `olist_order_payments_dataset.csv`

Payments applied to orders. An order can be paid with several instruments (e.g. a voucher plus a credit card), each one a separate record with its own sequence number.

* **Grain:** One record per payment instrument within an order (order_id, payment_sequential).
* **Primary key:** `order_id`, `payment_sequential`
* **Format:** utf-8, LF, delimiter `,`
* **Max reject ratio before CRITICAL:** 1.00%

| Column | Type | Nullable | Unique | Constraint | Meaning |
|--------|------|----------|--------|------------|---------|
| `order_id` | text | no |  | pattern `^[0-9a-f]{32}$`, max len 32 | Order being paid. Part of the primary key. |
| `payment_sequential` | integer | no |  |  | Sequence of the payment instrument within the order, starting at 1. |
| `payment_type` | text | no |  |  | Payment method (credit_card, boleto, voucher, debit_card, not_defined). |
| `payment_installments` | integer | no |  |  | Number of instalments chosen by the customer (credit card). |
| `payment_value` | numeric | no |  |  | Amount paid with this instrument, BRL. |

| Foreign key | References | Severity | Why |
|---|---|---|---|
| order_id | orders(order_id) | ERROR | A payment must belong to a known order. |

| Rule | Check | Severity | Why |
|---|---|---|---|
| `payment_type_accepted` | `payment_type` in {credit_card, boleto, voucher, debit_card, not_defined} | ERROR | Unknown payment types cannot be reported by method. |
| `payment_type_defined` | `payment_type <> 'not_defined'` | WARNING | The source uses 'not_defined' for a few payments; kept, reported as unknown method. |
| `payment_sequential_positive` | 1 ≤ `payment_sequential` | ERROR | Sequence numbers start at 1. |
| `payment_value_non_negative` | 0 ≤ `payment_value` | ERROR | A negative payment is not representable in this dataset (no refunds file). |
| `installments_non_negative` | 0 ≤ `payment_installments` | ERROR | A negative number of instalments is invalid. |
| `installments_at_least_one` | 1 ≤ `payment_installments` | WARNING | Zero instalments is observed in the source and is ambiguous; flagged, not dropped. |

## `order_reviews` ← `olist_order_reviews_dataset.csv`

Customer satisfaction surveys sent after delivery (or after the estimated delivery date). Free-text fields contain quoted multi-line text; the file uses CRLF line endings.

* **Grain:** One record per (review_id, order_id). review_id alone is NOT unique in the source: the same survey response can be linked to several orders (measured in docs/source_profile.md).
* **Primary key:** `review_id`, `order_id`
* **Format:** utf-8, CRLF, delimiter `,`
* **Max reject ratio before CRITICAL:** 1.00%

| Column | Type | Nullable | Unique | Constraint | Meaning |
|--------|------|----------|--------|------------|---------|
| `review_id` | text | no |  | pattern `^[0-9a-f]{32}$`, max len 32 | Survey response identifier. Part of the primary key. |
| `order_id` | text | no |  | pattern `^[0-9a-f]{32}$`, max len 32 | Order the review refers to. An order can have more than one review. |
| `review_score` | integer | no |  |  | Satisfaction score from 1 (worst) to 5 (best). |
| `review_comment_title` | text | yes |  |  | Optional title typed by the customer, Portuguese. |
| `review_comment_message` | text | yes |  |  | Optional free-text comment, Portuguese; may span several lines. |
| `review_creation_date` | timestamp | no |  |  | Date the survey was sent to the customer (time always 00:00:00). |
| `review_answer_timestamp` | timestamp | no |  |  | When the customer answered the survey. |

| Foreign key | References | Severity | Why |
|---|---|---|---|
| order_id | orders(order_id) | ERROR | A review must refer to a known order. |

| Rule | Check | Severity | Why |
|---|---|---|---|
| `review_score_in_range` | 1 ≤ `review_score` ≤ 5 | ERROR | The survey scale is 1..5; anything else is corrupt. |
| `answered_not_before_created` | `review_answer_timestamp >= review_creation_date` | ERROR | A survey cannot be answered before it is sent. |
| `review_not_in_future` | `review_answer_timestamp <= LOCALTIMESTAMP` | ERROR | Answers dated in the future are invalid. |

## `orders` ← `olist_orders_dataset.csv`

Order header: one record per purchase made on the Olist marketplace, with its lifecycle status and the timestamps of each fulfilment milestone. Hub table of the dataset.

* **Grain:** One record per order (order_id).
* **Primary key:** `order_id`
* **Format:** utf-8, LF, delimiter `,`
* **Max reject ratio before CRITICAL:** 1.00%

| Column | Type | Nullable | Unique | Constraint | Meaning |
|--------|------|----------|--------|------------|---------|
| `order_id` | text | no | yes | pattern `^[0-9a-f]{32}$`, max len 32 | Anonymised order identifier (32-char hex). Primary key. |
| `customer_id` | text | no | yes | pattern `^[0-9a-f]{32}$`, max len 32 | Per-order customer key into customers. Olist issues a new customer_id per order, so it is 1:1 with order_id; the person is identified by customers.customer_unique_id. |
| `order_status` | text | no |  |  | Lifecycle state at extraction time (delivered, shipped, canceled, ...). |
| `order_purchase_timestamp` | timestamp | no |  |  | When the customer placed the order. Local Brazilian time, no timezone in source. |
| `order_approved_at` | timestamp | yes |  |  | Payment approval time. Empty when payment was never approved (e.g. canceled). |
| `order_delivered_carrier_date` | timestamp | yes |  |  | When the seller handed the parcel to the logistics partner. |
| `order_delivered_customer_date` | timestamp | yes |  |  | Actual delivery to the customer. Empty for orders not (yet) delivered. |
| `order_estimated_delivery_date` | timestamp | no |  |  | Delivery date promised to the customer at purchase (date, time always 00:00:00). |

| Foreign key | References | Severity | Why |
|---|---|---|---|
| customer_id | customers(customer_id) | ERROR | Every order must belong to a known customer record. |

| Rule | Check | Severity | Why |
|---|---|---|---|
| `order_status_accepted` | `order_status` in {created, approved, invoiced, processing, shipped, delivered, canceled, unavailable} | ERROR | An unknown status cannot be mapped to delivery/cancellation metrics. |
| `approved_not_before_purchase` | `order_approved_at >= order_purchase_timestamp` | ERROR | Payment cannot be approved before the order exists. |
| `delivered_not_before_purchase` | `order_delivered_customer_date >= order_purchase_timestamp` | ERROR | A negative delivery lead time would corrupt delivery-performance metrics. |
| `carrier_not_before_purchase` | `order_delivered_carrier_date >= order_purchase_timestamp` | WARNING | Hand-off to carrier before purchase is physically impossible; observed in the source (see docs/source_profile.md), kept but flagged because customer-facing dates are valid. |
| `delivered_not_before_carrier` | `order_delivered_customer_date >= order_delivered_carrier_date` | WARNING | Delivery before carrier hand-off indicates a mis-keyed carrier date. |
| `delivered_status_has_delivery_date` | `order_status <> 'delivered' OR order_delivered_customer_date IS NOT NULL` | WARNING | Delivered orders without a delivery date are excluded from lead-time metrics. |
| `canceled_not_delivered` | `order_status <> 'canceled' OR order_delivered_customer_date IS NULL` | WARNING | A canceled order with a delivery date has contradictory status. |
| `purchase_not_in_future` | `order_purchase_timestamp <= LOCALTIMESTAMP` | ERROR | Orders dated in the future are invalid and would distort time series. |

## `product_category_translation` ← `product_category_name_translation.csv`

Lookup from Portuguese product category names to English. Unlike the other files it is encoded with a UTF-8 byte-order mark and CRLF line endings (measured, see source_profile).

* **Grain:** One record per Portuguese category name.
* **Primary key:** `product_category_name`
* **Format:** utf-8-sig, CRLF, delimiter `,`
* **Max reject ratio before CRITICAL:** 0.00%

| Column | Type | Nullable | Unique | Constraint | Meaning |
|--------|------|----------|--------|------------|---------|
| `product_category_name` | text | no | yes |  | Portuguese category name as used in products.product_category_name. |
| `product_category_name_english` | text | no |  |  | English translation used for reporting. |

## `products` ← `olist_products_dataset.csv`

Product catalogue: Portuguese category name and physical attributes used for freight. Two source column names are misspelled ("lenght") and are renamed in src.

* **Grain:** One record per product (product_id).
* **Primary key:** `product_id`
* **Format:** utf-8, LF, delimiter `,`
* **Max reject ratio before CRITICAL:** 1.00%

| Column | Type | Nullable | Unique | Constraint | Meaning |
|--------|------|----------|--------|------------|---------|
| `product_id` | text | no | yes | pattern `^[0-9a-f]{32}$`, max len 32 | Anonymised product identifier. Primary key. |
| `product_category_name` | text | yes |  |  | Portuguese category name. Empty for some products (category unknown). |
| `product_name_lenght` → `product_name_length` | integer | yes |  |  | Number of characters in the product name (source column name is misspelled). |
| `product_description_lenght` → `product_description_length` | integer | yes |  |  | Number of characters in the description (source column name is misspelled). |
| `product_photos_qty` | integer | yes |  |  | Number of published product photos. |
| `product_weight_g` | integer | yes |  |  | Weight in grams. |
| `product_length_cm` | integer | yes |  |  | Package length in centimetres. |
| `product_height_cm` | integer | yes |  |  | Package height in centimetres. |
| `product_width_cm` | integer | yes |  |  | Package width in centimetres. |

| Foreign key | References | Severity | Why |
|---|---|---|---|
| product_category_name | product_category_translation(product_category_name) | WARNING | Categories without an English translation keep their Portuguese name in dim_product instead of being dropped. |

| Rule | Check | Severity | Why |
|---|---|---|---|
| `weight_non_negative` | 0 ≤ `product_weight_g` | ERROR | Negative weight is invalid. |
| `weight_positive` | 1 ≤ `product_weight_g` | WARNING | Zero weight is observed and physically implausible; flagged for freight analysis. |
| `dimensions_positive` | `product_length_cm > 0 AND product_height_cm > 0 AND product_width_cm > 0` | ERROR | Package dimensions must be positive when present. |
| `photos_non_negative` | 0 ≤ `product_photos_qty` | ERROR | Photo count cannot be negative. |

## `sellers` ← `olist_sellers_dataset.csv`

Marketplace sellers that fulfil order items, with their location.

* **Grain:** One record per seller (seller_id).
* **Primary key:** `seller_id`
* **Format:** utf-8, LF, delimiter `,`
* **Max reject ratio before CRITICAL:** 1.00%

| Column | Type | Nullable | Unique | Constraint | Meaning |
|--------|------|----------|--------|------------|---------|
| `seller_id` | text | no | yes | pattern `^[0-9a-f]{32}$`, max len 32 | Anonymised seller identifier. Primary key. |
| `seller_zip_code_prefix` | text | no |  | pattern `^[0-9]{5}$`, max len 5 | First 5 digits of the seller CEP; text to preserve leading zeros. |
| `seller_city` | text | no |  |  | Seller city name as typed in the source (not normalised). |
| `seller_state` | text | no |  | max len 2 | Two-letter Brazilian federative unit (UF) code. |

| Foreign key | References | Severity | Why |
|---|---|---|---|
| seller_zip_code_prefix | geolocation(geolocation_zip_code_prefix) | WARNING | Existence check only; a few seller prefixes are absent from geolocation. |

| Rule | Check | Severity | Why |
|---|---|---|---|
| `seller_state_is_uf` | `seller_state` in {AC, AL, AM, AP, BA, CE, DF, ES, GO, MA, MG, MS, MT, PA, PB, PE, PI, PR, RJ, RN, RO, RR, RS, SC, SE, SP, TO} | ERROR | State must be one of the 27 Brazilian federative units. |
| `seller_city_has_no_digits` | `seller_city !~ '[0-9]'` | WARNING | A numeric city name (observed once) is a data-entry error; city reported as-is. |
