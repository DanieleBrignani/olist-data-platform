"""Typed, contract-conforming source tables (src) + issue classification in meta.

src.* is rebuilt atomically by `load_staging` from the latest loaded raw file per table.
Constraints here are the database's last line of defence: the staging rule engine already
quarantines violating records, so a constraint error means an engine bug and fails loudly.

* PK / FK exactly as in data_contracts (ERROR-severity FKs only; WARNING "FKs" such as
  zip prefix -> geolocation are existence checks, not relational keys).
* CHECK constraints for the invariants every downstream metric relies on.
* Indexes: FK columns used for joins and the purchase timestamp used for time filtering
  (justified with EXPLAIN ANALYZE in docs/query_plans.md).
* Lineage on every row: (_source_file_id, _row_number) of the first raw record, plus
  source_record_count = number of identical raw records collapsed into this row.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LINEAGE = """
    _source_file_id     bigint  NOT NULL REFERENCES meta.source_files,
    _row_number         bigint  NOT NULL,
    source_record_count integer NOT NULL DEFAULT 1 CHECK (source_record_count >= 1)"""

ZIP = "text NOT NULL CHECK ({c} ~ '^[0-9]{{5}}$')"
UF = "text NOT NULL CHECK ({c} ~ '^[A-Z]{{2}}$')"
ID = "text NOT NULL CHECK ({c} ~ '^[0-9a-f]{{32}}$')"

# Order matters: parents before children
TABLES: dict[str, str] = {
    "geolocation": f"""
    geolocation_zip_code_prefix {ZIP.format(c="geolocation_zip_code_prefix")},
    geolocation_lat   numeric NOT NULL CHECK (geolocation_lat BETWEEN -90 AND 90),
    geolocation_lng   numeric NOT NULL CHECK (geolocation_lng BETWEEN -180 AND 180),
    geolocation_city  text NOT NULL,
    geolocation_state {UF.format(c="geolocation_state")},{LINEAGE},
    CONSTRAINT uq_geolocation_point UNIQUE (geolocation_zip_code_prefix, geolocation_lat,
        geolocation_lng, geolocation_city, geolocation_state)""",
    "product_category_translation": f"""
    product_category_name         text PRIMARY KEY,
    product_category_name_english text NOT NULL,{LINEAGE}""",
    "customers": f"""
    customer_id              {ID.format(c="customer_id")} PRIMARY KEY,
    customer_unique_id       {ID.format(c="customer_unique_id")},
    customer_zip_code_prefix {ZIP.format(c="customer_zip_code_prefix")},
    customer_city            text NOT NULL,
    customer_state           {UF.format(c="customer_state")},{LINEAGE}""",
    "sellers": f"""
    seller_id              {ID.format(c="seller_id")} PRIMARY KEY,
    seller_zip_code_prefix {ZIP.format(c="seller_zip_code_prefix")},
    seller_city            text NOT NULL,
    seller_state           {UF.format(c="seller_state")},{LINEAGE}""",
    "products": f"""
    product_id                 {ID.format(c="product_id")} PRIMARY KEY,
    product_category_name      text,
    product_name_length        integer CHECK (product_name_length >= 0),
    product_description_length integer CHECK (product_description_length >= 0),
    product_photos_qty         integer CHECK (product_photos_qty >= 0),
    product_weight_g           integer CHECK (product_weight_g >= 0),
    product_length_cm          integer CHECK (product_length_cm > 0),
    product_height_cm          integer CHECK (product_height_cm > 0),
    product_width_cm           integer CHECK (product_width_cm > 0),{LINEAGE}""",
    "orders": f"""
    order_id                      {ID.format(c="order_id")} PRIMARY KEY,
    customer_id                   text NOT NULL UNIQUE REFERENCES src.customers,
    order_status                  text NOT NULL CHECK (order_status IN ('created', 'approved',
        'invoiced', 'processing', 'shipped', 'delivered', 'canceled', 'unavailable')),
    order_purchase_timestamp      timestamp NOT NULL,
    order_approved_at             timestamp,
    order_delivered_carrier_date  timestamp,
    order_delivered_customer_date timestamp,
    order_estimated_delivery_date timestamp NOT NULL,{LINEAGE},
    CHECK (order_delivered_customer_date >= order_purchase_timestamp)""",
    "order_items": f"""
    order_id            text NOT NULL REFERENCES src.orders,
    order_item_id       integer NOT NULL CHECK (order_item_id >= 1),
    product_id          text NOT NULL REFERENCES src.products,
    seller_id           text NOT NULL REFERENCES src.sellers,
    shipping_limit_date timestamp NOT NULL,
    price               numeric NOT NULL CHECK (price > 0),
    freight_value       numeric NOT NULL CHECK (freight_value >= 0),{LINEAGE},
    PRIMARY KEY (order_id, order_item_id)""",
    "order_payments": f"""
    order_id             text NOT NULL REFERENCES src.orders,
    payment_sequential   integer NOT NULL CHECK (payment_sequential >= 1),
    payment_type         text NOT NULL,
    payment_installments integer NOT NULL CHECK (payment_installments >= 0),
    payment_value        numeric NOT NULL CHECK (payment_value >= 0),{LINEAGE},
    PRIMARY KEY (order_id, payment_sequential)""",
    "order_reviews": f"""
    review_id               {ID.format(c="review_id")},
    order_id                text NOT NULL REFERENCES src.orders,
    review_score            integer NOT NULL CHECK (review_score BETWEEN 1 AND 5),
    review_comment_title    text,
    review_comment_message  text,
    review_creation_date    timestamp NOT NULL,
    review_answer_timestamp timestamp NOT NULL,{LINEAGE},
    PRIMARY KEY (review_id, order_id)""",
}

INDEXES = [
    # FK columns not already leading a PK/unique index: used by every fact join
    "CREATE INDEX ix_src_order_items_product_id ON src.order_items (product_id)",
    "CREATE INDEX ix_src_order_items_seller_id ON src.order_items (seller_id)",
    "CREATE INDEX ix_src_order_reviews_order_id ON src.order_reviews (order_id)",
    # Time-range filtering on the hub table
    "CREATE INDEX ix_src_orders_purchase_ts ON src.orders (order_purchase_timestamp)",
    # Existence checks zip -> geolocation
    "CREATE INDEX ix_src_geolocation_zip ON src.geolocation (geolocation_zip_code_prefix)",
]


def upgrade() -> None:
    op.execute(
        "ALTER TABLE meta.rejected_records "
        "ADD COLUMN layer text NOT NULL DEFAULT 'raw' CHECK (layer IN ('raw', 'src')), "
        "ADD COLUMN action text NOT NULL DEFAULT 'quarantined' "
        "CHECK (action IN ('quarantined', 'flagged')), "
        "ADD COLUMN source_row_number bigint, "
        "ADD CONSTRAINT ck_rejected_action_severity "
        "CHECK ((action = 'flagged') = (severity = 'WARNING'))"
    )
    op.execute(
        "CREATE INDEX ix_rejected_records_file_row "
        "ON meta.rejected_records (source_file_id, source_row_number)"
    )
    op.execute("""
    CREATE VIEW meta.current_source_files AS
    SELECT DISTINCT ON (source_table) *
    FROM meta.source_files
    WHERE status = 'loaded'
    ORDER BY source_table, loaded_at DESC, source_file_id DESC""")

    for table, body in TABLES.items():
        op.execute(f"CREATE TABLE src.{table} ({body}\n)")
    for statement in INDEXES:
        op.execute(statement)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE src.{table}")
    op.execute("DROP VIEW meta.current_source_files")
    op.execute("DROP INDEX meta.ix_rejected_records_file_row")
    op.execute(
        "ALTER TABLE meta.rejected_records DROP CONSTRAINT ck_rejected_action_severity, "
        "DROP COLUMN source_row_number, DROP COLUMN action, DROP COLUMN layer"
    )
