"""Pipeline metadata tables (meta) and raw landing tables (raw).

raw.* tables hold every structurally valid record of a file exactly as text (no casting,
no trimming, empty string preserved), plus lineage columns. Column lists are written out
literally so this migration never changes when a contract changes; a unit test asserts
they match data_contracts/*.yml.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RAW_TABLES: dict[str, list[str]] = {
    "customers": [
        "customer_id",
        "customer_unique_id",
        "customer_zip_code_prefix",
        "customer_city",
        "customer_state",
    ],
    "geolocation": [
        "geolocation_zip_code_prefix",
        "geolocation_lat",
        "geolocation_lng",
        "geolocation_city",
        "geolocation_state",
    ],
    "order_items": [
        "order_id",
        "order_item_id",
        "product_id",
        "seller_id",
        "shipping_limit_date",
        "price",
        "freight_value",
    ],
    "order_payments": [
        "order_id",
        "payment_sequential",
        "payment_type",
        "payment_installments",
        "payment_value",
    ],
    "order_reviews": [
        "review_id",
        "order_id",
        "review_score",
        "review_comment_title",
        "review_comment_message",
        "review_creation_date",
        "review_answer_timestamp",
    ],
    "orders": [
        "order_id",
        "customer_id",
        "order_status",
        "order_purchase_timestamp",
        "order_approved_at",
        "order_delivered_carrier_date",
        "order_delivered_customer_date",
        "order_estimated_delivery_date",
    ],
    "product_category_translation": ["product_category_name", "product_category_name_english"],
    "products": [
        "product_id",
        "product_category_name",
        "product_name_lenght",
        "product_description_lenght",
        "product_photos_qty",
        "product_weight_g",
        "product_length_cm",
        "product_height_cm",
        "product_width_cm",
    ],
    "sellers": ["seller_id", "seller_zip_code_prefix", "seller_city", "seller_state"],
}


def upgrade() -> None:
    op.execute("""
    CREATE TABLE meta.pipeline_runs (
        pipeline_run_id  uuid PRIMARY KEY,
        flow_name        text        NOT NULL,
        environment      text        NOT NULL,
        dataset_version  integer     NOT NULL,
        status           text        NOT NULL
            CHECK (status IN ('running', 'success', 'failed')),
        started_at       timestamptz NOT NULL DEFAULT now(),
        finished_at      timestamptz,
        failed_task      text,
        error_type       text,
        error_message    text,
        CHECK ((status = 'running') = (finished_at IS NULL)),
        CHECK (status <> 'failed' OR error_type IS NOT NULL)
    )""")
    op.execute(
        "CREATE INDEX ix_pipeline_runs_status_finished ON meta.pipeline_runs (status, finished_at)"
    )

    op.execute("""
    CREATE TABLE meta.source_files (
        source_file_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        source_table       text        NOT NULL,
        file_name          text        NOT NULL,
        sha256             char(64)    NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
        size_bytes         bigint      NOT NULL CHECK (size_bytes >= 0),
        expected_rows      bigint      NOT NULL CHECK (expected_rows >= 0),
        dataset_version    integer     NOT NULL,
        status             text        NOT NULL
            CHECK (status IN ('loading', 'loaded', 'failed')),
        rows_loaded        bigint      CHECK (rows_loaded >= 0),
        rows_rejected      bigint      CHECK (rows_rejected >= 0),
        registered_run_id  uuid        NOT NULL REFERENCES meta.pipeline_runs,
        loaded_run_id      uuid        REFERENCES meta.pipeline_runs,
        registered_at      timestamptz NOT NULL DEFAULT now(),
        loaded_at          timestamptz,
        CONSTRAINT uq_source_files_table_sha UNIQUE (source_table, sha256),
        CHECK (status <> 'loaded'
               OR (rows_loaded IS NOT NULL AND rows_rejected IS NOT NULL AND loaded_at IS NOT NULL))
    )""")

    op.execute("""
    CREATE TABLE meta.ingestion_events (
        event_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        pipeline_run_id  uuid        NOT NULL REFERENCES meta.pipeline_runs,
        source_file_id   bigint      REFERENCES meta.source_files,
        task             text        NOT NULL,
        source           text,
        event            text        NOT NULL,
        status           text        NOT NULL CHECK (status IN ('ok', 'skipped', 'failed')),
        rows_read        bigint,
        rows_written     bigint,
        rows_rejected    bigint,
        duration_ms      numeric(12, 1),
        details          jsonb       NOT NULL DEFAULT '{}'::jsonb,
        created_at       timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE INDEX ix_ingestion_events_run ON meta.ingestion_events (pipeline_run_id)")

    op.execute("""
    CREATE TABLE meta.rejected_records (
        rejected_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        pipeline_run_id  uuid        NOT NULL REFERENCES meta.pipeline_runs,
        source_file_id   bigint      REFERENCES meta.source_files,
        source_table     text        NOT NULL,
        record_ref       text        NOT NULL,
        rule_name        text        NOT NULL,
        severity         text        NOT NULL CHECK (severity IN ('WARNING', 'ERROR', 'CRITICAL')),
        reason           text        NOT NULL,
        raw_record       jsonb,
        rejected_at      timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE INDEX ix_rejected_records_run ON meta.rejected_records (pipeline_run_id)")
    op.execute(
        "CREATE INDEX ix_rejected_records_rule ON meta.rejected_records (source_table, rule_name)"
    )

    for table, columns in RAW_TABLES.items():
        column_ddl = ",\n        ".join(f"{c} text" for c in columns)
        op.execute(f"""
        CREATE TABLE raw.{table} (
            _source_file_id  bigint      NOT NULL REFERENCES meta.source_files,
            _row_number      bigint      NOT NULL CHECK (_row_number >= 1),
            _pipeline_run_id uuid        NOT NULL REFERENCES meta.pipeline_runs,
            _loaded_at       timestamptz NOT NULL DEFAULT now(),
            {column_ddl},
            PRIMARY KEY (_source_file_id, _row_number)
        )""")


def downgrade() -> None:
    for table in RAW_TABLES:
        op.execute(f"DROP TABLE raw.{table}")
    for table in ("rejected_records", "ingestion_events", "source_files", "pipeline_runs"):
        op.execute(f"DROP TABLE meta.{table}")
