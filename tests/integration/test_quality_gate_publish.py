"""Quality gate -> publication, end to end on the real dbt project (synthetic data).

FAILURE TEST (required by the brief): inject ONE invalid source condition that no
single-file contract can see - an order line whose shipping deadline precedes the order's
purchase - and prove that:
  * ingestion and staging accept it (it is valid per every file contract),
  * the CRITICAL warehouse rule catches it and the quality gate FAILS,
  * the previously published warehouse/marts stay exactly as they were,
  * the evidence (dq result, failing row, gate decision, failed run) is recorded.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from olist_platform.config import Role
from olist_platform.database.engine import get_engine
from olist_platform.database.publish import publish, rollback
from olist_platform.errors import QualityGateError
from olist_platform.ingestion.manifest import Lock, LockEntry, build_manifest
from olist_platform.ingestion.pipeline import run_ingestion, run_staging
from olist_platform.ingestion.runs import start_run
from olist_platform.transform.warehouse import WarehouseSummary, run_warehouse
from olist_platform.validation.contracts import load_contracts
from tests.synthetic import base_rows, order, prod, seller, write_dataset

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]

CONTRACTS = load_contracts()


def build(tmp_path: Path, rows: dict, name: str) -> WarehouseSummary:
    directory = write_dataset(tmp_path / name, rows, CONTRACTS)
    entries = build_manifest(directory, CONTRACTS)
    lock = Lock(
        dataset="synthetic",
        dataset_version=2,
        files=[LockEntry(**e.model_dump(include=set(LockEntry.model_fields))) for e in entries],
    )
    engine = get_engine(Role.PIPELINE)
    run_ingestion(engine, directory, CONTRACTS, environment="test", lock=lock)
    run_staging(engine, CONTRACTS, environment="test")
    return run_warehouse(
        engine, environment="test", dataset_version=2, target_path=str(tmp_path / f"target_{name}")
    )


def q(sql: str, role: Role = Role.PIPELINE, **params):
    with get_engine(role).connect() as conn:
        return conn.execute(text(sql), params).all()


def published_fingerprint() -> list:
    return q(
        "SELECT (SELECT count(*) FROM warehouse.fct_order_items), "
        "(SELECT sum(gmv) FROM marts.mart_sales), "
        "(SELECT count(*) FROM meta.publications WHERE action = 'publish')"
    )


def test_clean_build_passes_gate_and_is_readable_by_reporting(tmp_path: Path) -> None:
    summary = build(tmp_path, base_rows(), "clean")

    assert summary.decision.passed
    assert summary.published_rows["warehouse.fct_orders"] == 3
    # consumers read the PUBLISHED schemas with the reporting role
    assert q("SELECT sum(orders) FROM marts.mart_sales", Role.REPORTING) == [(3,)]
    assert q("SELECT count(*) FROM warehouse.fct_order_items", Role.REPORTING) == [(3,)]
    assert q(
        "SELECT decision FROM meta.quality_gate_decisions WHERE pipeline_run_id = :r",
        r=summary.pipeline_run_id,
    ) == [("PASS",)]
    assert q(
        "SELECT count(*) FROM meta.dq_results WHERE pipeline_run_id = :r AND blocking",
        r=summary.pipeline_run_id,
    ) == [(0,)]


def test_injected_invalid_source_condition_blocks_publication(tmp_path: Path) -> None:
    build(tmp_path, base_rows(), "good")
    before = published_fingerprint()

    bad = base_rows()
    bad["order_items"].append(  # extra line on order 1: deadline BEFORE the purchase
        {
            "order_id": order(1),
            "order_item_id": "2",
            "product_id": prod(2),
            "seller_id": seller(1),
            "shipping_limit_date": "2017-12-01 00:00:00",
            "price": "10.00",
            "freight_value": "1.00",
        }
    )
    with pytest.raises(QualityGateError, match="shipping_limit_not_before_purchase"):
        build(tmp_path, bad, "bad")

    # 1. staging accepted it: valid per every single-file contract
    assert q("SELECT count(*) FROM src.order_items") == [(4,)]
    assert q("SELECT count(*) FROM meta.rejected_records WHERE layer IN ('raw', 'src')") == [(0,)]
    # 2. published data is exactly as before the bad run
    assert published_fingerprint() == before
    assert q("SELECT count(*) FROM warehouse.fct_order_items", Role.REPORTING) == [(3,)]
    # 3. evidence: failed run at quality_gate, FAIL decision, blocking CRITICAL result,
    #    and the offending row itself stored in the quarantine table
    run_id, failed_task = q(
        "SELECT pipeline_run_id, failed_task FROM meta.pipeline_runs "
        "WHERE flow_name = 'olist_warehouse' AND status = 'failed'"
    )[0]
    assert failed_task == "quality_gate"
    assert q(
        "SELECT decision, blocking_failures FROM meta.quality_gate_decisions "
        "WHERE pipeline_run_id = :r",
        r=run_id,
    ) == [("FAIL", 1)]
    assert q(
        "SELECT severity, failures FROM meta.dq_results WHERE pipeline_run_id = :r AND blocking",
        r=run_id,
    ) == [("CRITICAL", 1)]
    evidence = q(
        "SELECT rule_name, severity, action, raw_record->>'order_id', "
        "raw_record->>'order_item_id' FROM meta.rejected_records "
        "WHERE layer = 'warehouse' AND pipeline_run_id = :r ORDER BY rule_name",
        r=run_id,
    )
    assert evidence == [
        # the extra R$11 line also leaves order 1 under-paid: ERROR rule, within its
        # tolerance, so non-blocking - but still recorded (nothing disappears silently)
        ("assert_payments_cover_order_value", "ERROR", "reported", order(1), None),
        # the blocking CRITICAL finding, pointing at the exact injected line
        ("assert_shipping_limit_not_before_purchase", "CRITICAL", "reported", order(1), "2"),
    ]


def test_publish_refuses_without_a_pass_decision() -> None:
    run_id = start_run(get_engine(Role.PIPELINE), "manual", "test", 2)
    with pytest.raises(QualityGateError, match="refusing to publish"):
        publish(get_engine(Role.PIPELINE), run_id)


def test_rollback_restores_previous_publication_and_revokes_prev(tmp_path: Path) -> None:
    build(tmp_path, base_rows(), "v1")
    bigger = base_rows()
    bigger["order_items"].append(
        {
            "order_id": order(2),
            "order_item_id": "2",
            "product_id": prod(1),
            "seller_id": seller(2),
            "shipping_limit_date": "2018-01-10 00:00:00",
            "price": "20.00",
            "freight_value": "2.00",
        }
    )
    build(tmp_path, bigger, "v2")
    assert q("SELECT count(*) FROM warehouse.fct_order_items", Role.REPORTING) == [(4,)]
    # the demoted version must not stay readable by consumers
    assert q("SELECT has_schema_privilege('olist_reporting', 'warehouse_prev', 'USAGE')") == [
        (False,)
    ]

    engine = get_engine(Role.PIPELINE)
    rollback(engine, start_run(engine, "olist_rollback", "test", 2))

    assert q("SELECT count(*) FROM warehouse.fct_order_items", Role.REPORTING) == [(3,)]
    assert q("SELECT count(*) FROM warehouse_prev.fct_order_items") == [(4,)]
    assert q("SELECT action FROM meta.publications ORDER BY publication_id DESC LIMIT 1") == [
        ("rollback",)
    ]


def test_reporting_role_is_read_only_on_published_schemas(tmp_path: Path) -> None:
    build(tmp_path, base_rows(), "ro")
    with get_engine(Role.REPORTING).connect() as conn, pytest.raises(Exception, match="denied"):
        conn.execute(text("DELETE FROM marts.mart_sales"))
    assert uuid.UUID(str(q("SELECT pipeline_run_id FROM meta.publications LIMIT 1")[0][0]))
