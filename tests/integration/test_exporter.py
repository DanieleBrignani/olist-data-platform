"""The exporter reports exactly what the metadata tables contain (read as olist_monitor)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from prometheus_client import CollectorRegistry, generate_latest
from sqlalchemy import create_engine, text

from olist_platform.config import Role, get_settings
from olist_platform.database.engine import get_engine
from olist_platform.observability.exporter import MetadataCollector

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]

T0 = dt.datetime(2026, 9, 27, 10, 0, tzinfo=dt.UTC)


def seed() -> None:
    ok, failed = uuid.uuid4(), uuid.uuid4()
    with get_engine(Role.PIPELINE).begin() as conn:
        conn.execute(
            text(
                "INSERT INTO meta.pipeline_runs (pipeline_run_id, flow_name, environment, "
                "dataset_version, status, started_at, finished_at, failed_task, error_type) VALUES "
                "(:ok, 'olist_refresh', 'test', 2, 'success', :t0, :t0 + interval '100 seconds', "
                "NULL, NULL), "
                "(:failed, 'olist_refresh', 'test', 2, 'failed', :t0, :t0 + interval '40 seconds', "
                "'quality_gate', 'quality_gate_failed'), "
                "(:running, 'olist_refresh', 'test', 2, 'running', :t0, NULL, NULL, NULL)"
            ),
            {"ok": ok, "failed": failed, "running": uuid.uuid4(), "t0": T0},
        )
        conn.execute(
            text(
                "INSERT INTO meta.ingestion_events (pipeline_run_id, task, source, event, status, "
                "rows_read, rows_written) VALUES "
                "(:ok, 'ingest_raw', 'olist_orders_dataset.csv', 'file_loaded', 'ok', 100, 99), "
                "(:ok, 'load_staging', 'orders', 'table_staged', 'ok', 99, 98), "
                "(:ok, 'ingest_raw', 'olist_orders_dataset.csv', 'skipped_already_loaded', "
                "'skipped', NULL, NULL)"
            ),
            {"ok": ok},
        )
        conn.execute(
            text(
                "INSERT INTO meta.rejected_records (pipeline_run_id, source_table, record_ref, "
                "rule_name, severity, reason, layer, action) VALUES "
                "(:ok, 'orders', 'r#1', 'order_status_accepted', 'ERROR', 'x', 'src', "
                "'quarantined'),"
                "(:ok, 'orders', 'r#2', 'carrier_not_before_purchase', 'WARNING', 'x', 'src', "
                "'flagged')"
            ),
            {"ok": ok},
        )
        conn.execute(
            text(
                "INSERT INTO meta.dq_results (pipeline_run_id, test_unique_id, test_name, model, "
                "severity, status, failures, tolerance, blocking) VALUES "
                "(:failed, 'test.a', 'assert_x', 'fct_orders', 'CRITICAL', 'fail', 1, 0, true), "
                "(:ok, 'test.b', 'assert_y', 'fct_orders', 'WARNING', 'warn', 3, 0, false), "
                "(:ok, 'test.c', 'assert_z', 'fct_orders', 'CRITICAL', 'pass', 0, 0, false)"
            ),
            {"ok": ok, "failed": failed},
        )
        conn.execute(
            text(
                "INSERT INTO meta.quality_gate_decisions (pipeline_run_id, decision, "
                "tests_evaluated, blocking_failures, warnings) VALUES (:ok, 'PASS', 3, 0, 1)"
            ),
            {"ok": ok},
        )
        conn.execute(
            text(
                "INSERT INTO meta.publications (pipeline_run_id, action, published_at, "
                "source_max_event_at) VALUES (:ok, 'publish', :t0, '2018-10-17 17:30:18')"
            ),
            {"ok": ok, "t0": T0},
        )


def scrape(engine) -> dict[str, float]:
    registry = CollectorRegistry(auto_describe=False)
    registry.register(MetadataCollector(engine))
    samples = {}
    for family in registry.collect():
        for s in family.samples:
            labels = ",".join(f"{k}={v}" for k, v in sorted(s.labels.items()))
            samples[f"{s.name}{{{labels}}}"] = s.value
    return samples


def test_metrics_reflect_the_metadata_tables() -> None:
    seed()
    m = scrape(get_engine(Role.MONITOR))

    assert m["olist_exporter_db_up{}"] == 1
    assert m["pipeline_runs_total{flow=olist_refresh,status=success}"] == 1
    assert m["pipeline_runs_total{flow=olist_refresh,status=failed}"] == 1
    assert m["pipeline_runs_in_progress{flow=olist_refresh}"] == 1  # running is not "finished"
    assert (
        m[
            "pipeline_failures_total{error_type=quality_gate_failed,failed_task=quality_gate,"
            "flow=olist_refresh}"
        ]
        == 1
    )
    assert m["pipeline_duration_seconds_count{flow=olist_refresh}"] == 2
    assert m["pipeline_duration_seconds_sum{flow=olist_refresh}"] == 140
    assert m["pipeline_duration_seconds_bucket{flow=olist_refresh,le=60.0}"] == 1
    assert m["pipeline_duration_seconds_bucket{flow=olist_refresh,le=120.0}"] == 2
    assert m["rows_processed_total{source=olist_orders_dataset.csv,stage=ingest_raw}"] == 100
    assert m["rows_processed_total{source=orders,stage=load_staging}"] == 98
    assert (
        m["rows_rejected_total{layer=src,rule=order_status_accepted,severity=ERROR,source=orders}"]
        == 1
    )
    assert m["rows_flagged_total{layer=src,rule=carrier_not_before_purchase,source=orders}"] == 1
    assert (
        m[
            "data_quality_failures_total{blocking=true,model=fct_orders,rule=assert_x,"
            "severity=CRITICAL}"
        ]
        == 1
    )
    assert not any("assert_z" in k for k in m)  # passing tests are not failures
    assert m["quality_gate_last_decision{}"] == 1
    assert m["dataset_last_published_timestamp_seconds{}"] == T0.timestamp()
    assert (
        m["source_max_event_timestamp_seconds{}"]
        == dt.datetime(2018, 10, 17, 17, 30, 18, tzinfo=dt.UTC).timestamp()
    )


def test_empty_metadata_is_not_an_error() -> None:
    m = scrape(get_engine(Role.MONITOR))
    assert m["olist_exporter_db_up{}"] == 1
    assert not any(k.startswith("pipeline_runs_total") for k in m)


def test_database_outage_reports_down_instead_of_crashing() -> None:
    url = get_settings().database_url(Role.MONITOR).set(port=1)
    broken = create_engine(url, connect_args={"connect_timeout": 2})
    registry = CollectorRegistry(auto_describe=False)
    registry.register(MetadataCollector(broken))
    body = generate_latest(registry).decode()
    assert "olist_exporter_db_up 0.0" in body
