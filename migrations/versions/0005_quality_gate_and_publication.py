"""Data-quality results, quality-gate decisions and publication history.

* meta.dq_results               one row per dbt test per run (severity, status, failures)
* meta.quality_gate_decisions   PASS/FAIL per run with the blocking reasons
* meta.publications             every successful schema swap (feeds freshness metrics)
* meta.rejected_records         now also holds a sample of failing WAREHOUSE rows
                                (layer='warehouse', action='reported': the rows are not
                                removed from the model, they block or annotate publication)

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE meta.dq_results (
        dq_result_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        pipeline_run_id  uuid        NOT NULL REFERENCES meta.pipeline_runs,
        test_unique_id   text        NOT NULL,
        test_name        text        NOT NULL,
        model            text,
        severity         text        NOT NULL CHECK (severity IN ('WARNING', 'ERROR', 'CRITICAL')),
        status           text        NOT NULL CHECK (status IN ('pass', 'warn', 'fail', 'error', 'skipped')),
        failures         bigint      CHECK (failures >= 0),
        tolerance        bigint      NOT NULL DEFAULT 0 CHECK (tolerance >= 0),
        blocking         boolean     NOT NULL,
        message          text,
        failures_relation text,
        duration_ms      numeric(12, 1),
        executed_at      timestamptz NOT NULL DEFAULT now(),
        UNIQUE (pipeline_run_id, test_unique_id)
    )""")
    op.execute("CREATE INDEX ix_dq_results_status ON meta.dq_results (severity, status)")

    op.execute("""
    CREATE TABLE meta.quality_gate_decisions (
        pipeline_run_id  uuid        PRIMARY KEY REFERENCES meta.pipeline_runs,
        decision         text        NOT NULL CHECK (decision IN ('PASS', 'FAIL')),
        tests_evaluated  integer     NOT NULL,
        blocking_failures integer    NOT NULL,
        warnings         integer     NOT NULL,
        reasons          jsonb       NOT NULL DEFAULT '[]'::jsonb,
        decided_at       timestamptz NOT NULL DEFAULT now(),
        CHECK ((decision = 'PASS') = (blocking_failures = 0))
    )""")

    op.execute("""
    CREATE TABLE meta.publications (
        publication_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        pipeline_run_id  uuid        NOT NULL REFERENCES meta.pipeline_runs,
        action           text        NOT NULL CHECK (action IN ('publish', 'rollback')),
        published_at     timestamptz NOT NULL DEFAULT now(),
        table_row_counts jsonb       NOT NULL DEFAULT '{}'::jsonb,
        source_max_event_at timestamp
    )""")
    op.execute("CREATE INDEX ix_publications_time ON meta.publications (published_at)")

    op.execute("ALTER TABLE meta.rejected_records DROP CONSTRAINT ck_rejected_action_severity")
    op.execute("ALTER TABLE meta.rejected_records DROP CONSTRAINT rejected_records_layer_check")
    op.execute("ALTER TABLE meta.rejected_records DROP CONSTRAINT rejected_records_action_check")
    op.execute("""
    ALTER TABLE meta.rejected_records
        ADD CONSTRAINT rejected_records_layer_check
            CHECK (layer IN ('raw', 'src', 'warehouse')),
        ADD CONSTRAINT rejected_records_action_check
            CHECK (action IN ('quarantined', 'flagged', 'reported')),
        ADD CONSTRAINT ck_rejected_action_by_layer CHECK (
            (layer IN ('raw', 'src') AND (action = 'flagged') = (severity = 'WARNING')
                AND action <> 'reported')
            OR (layer = 'warehouse' AND action = 'reported')
        )""")


def downgrade() -> None:
    op.execute("DELETE FROM meta.rejected_records WHERE layer = 'warehouse'")
    op.execute("ALTER TABLE meta.rejected_records DROP CONSTRAINT ck_rejected_action_by_layer")
    op.execute("ALTER TABLE meta.rejected_records DROP CONSTRAINT rejected_records_layer_check")
    op.execute("ALTER TABLE meta.rejected_records DROP CONSTRAINT rejected_records_action_check")
    op.execute("""
    ALTER TABLE meta.rejected_records
        ADD CONSTRAINT rejected_records_layer_check CHECK (layer IN ('raw', 'src')),
        ADD CONSTRAINT rejected_records_action_check CHECK (action IN ('quarantined', 'flagged')),
        ADD CONSTRAINT ck_rejected_action_severity
            CHECK ((action = 'flagged') = (severity = 'WARNING'))""")
    op.execute("DROP TABLE meta.publications")
    op.execute("DROP TABLE meta.quality_gate_decisions")
    op.execute("DROP TABLE meta.dq_results")
