"""Incremental behaviour of the real `olist_refresh` flow (docs/incremental.md).

"Incremental" in this project means: the unit of change is a source-file SNAPSHOT (sha256);
the pipeline rebuilds only when the snapshot set or the transformation logic differs from the
published version; each table's latest snapshot is its complete current state.

Synthetic, contract-shaped snapshots v1 -> v2 exercise every case the policy must handle.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from prefect.testing.utilities import prefect_test_harness
from sqlalchemy import text

from olist_platform.config import Role
from olist_platform.database import change_detection
from olist_platform.database.engine import get_engine
from olist_platform.database.fingerprint import diff, fingerprint
from olist_platform.database.publish import rollback
from olist_platform.ingestion.runs import start_run
from olist_platform.validation.contracts import load_contracts
from orchestration.olist_flow import olist_refresh
from tests.synthetic import (
    base_rows,
    cust,
    hexid,
    order,
    prod,
    review,
    seller,
    write_versioned_source,
)

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.usefixtures("clean_db")]

CONTRACTS = load_contracts()
DERIVED = (
    "stg",
    "int",
    "warehouse_build",
    "marts_build",
    "warehouse",
    "marts",
    "warehouse_prev",
    "marts_prev",
    "dq_failures",
)


@pytest.fixture(scope="module", autouse=True)
def prefect_backend() -> Iterator[None]:
    with prefect_test_harness():
        yield


@pytest.fixture(autouse=True)
def _dbt_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DBT_TARGET_PATH", str(tmp_path / "dbt_target"))


def v2_rows() -> dict:
    """v1 plus: one NEW order (with customer, item, payment, review), one MODIFIED review
    score, and one DELETED order (order 3 and its children are absent from v2)."""
    rows = base_rows()
    rows["customers"].append(
        {
            "customer_id": cust(4),
            "customer_unique_id": hexid(9, 4),
            "customer_zip_code_prefix": "01003",
            "customer_city": "sao paulo",
            "customer_state": "SP",
        }
    )
    rows["orders"].append(
        {
            "order_id": order(4),
            "customer_id": cust(4),
            "order_status": "delivered",
            "order_purchase_timestamp": "2018-02-01 10:00:00",
            "order_approved_at": "2018-02-01 11:00:00",
            "order_delivered_carrier_date": "2018-02-02 09:00:00",
            "order_delivered_customer_date": "2018-02-05 18:00:00",
            "order_estimated_delivery_date": "2018-02-20 00:00:00",
        }
    )
    rows["order_items"].append(
        {
            "order_id": order(4),
            "order_item_id": "1",
            "product_id": prod(1),
            "seller_id": seller(1),
            "shipping_limit_date": "2018-02-03 00:00:00",
            "price": "80.00",
            "freight_value": "10.00",
        }
    )
    rows["order_payments"].append(
        {
            "order_id": order(4),
            "payment_sequential": "1",
            "payment_type": "boleto",
            "payment_installments": "1",
            "payment_value": "90.00",
        }
    )
    rows["order_reviews"].append(
        {
            "review_id": review(4),
            "order_id": order(4),
            "review_score": "4",
            "review_comment_title": "",
            "review_comment_message": "",
            "review_creation_date": "2018-02-06 00:00:00",
            "review_answer_timestamp": "2018-02-07 08:00:00",
        }
    )
    rows["order_reviews"][0]["review_score"] = "2"  # modified
    for table in ("orders", "order_items", "order_payments", "order_reviews"):  # deleted
        rows[table] = [r for r in rows[table] if r["order_id"] != order(3)]
    rows["customers"] = [r for r in rows["customers"] if r["customer_id"] != cust(3)]
    return rows


def q(sql: str, role: Role = Role.PIPELINE, **params):
    with get_engine(role).connect() as conn:
        return conn.execute(text(sql), params).all()


def published() -> dict:
    with get_engine(Role.REPORTING).connect() as conn:
        return fingerprint(conn)


def staged(run_id: str) -> int:
    return q(
        "SELECT count(*) FROM meta.ingestion_events WHERE task = 'load_staging' "
        "AND pipeline_run_id = :r",
        r=run_id,
    )[0][0]


def run(root_lock: tuple[str, str], **kw) -> dict:
    root, lock = root_lock
    return olist_refresh(data_root=root, lock_path=lock, **kw)


def reset_everything() -> None:
    with get_engine(Role.PIPELINE).begin() as conn:
        for schema in DERIVED:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
    with get_engine(Role.ADMIN).begin() as conn:
        tables = (
            conn.execute(
                text(
                    "SELECT schemaname || '.' || tablename FROM pg_tables "
                    "WHERE schemaname IN ('meta', 'raw', 'src')"
                )
            )
            .scalars()
            .all()
        )
        conn.execute(text(f"TRUNCATE {', '.join(tables)} CASCADE"))


# ------------------------------------------------------------------------------ 1 & 2


def test_first_run_loads_everything_and_identical_rerun_changes_nothing(tmp_path: Path) -> None:
    v1 = write_versioned_source(tmp_path / "v1", base_rows(), CONTRACTS)

    first = run(v1)
    after_first = published()
    second = run(v1)

    assert first["changes"]["reason"] == "never_published"
    assert first["ingest"]["loaded"] == 9 and first["published_tables"] == 16
    assert q("SELECT count(*) FROM warehouse.fct_orders", Role.REPORTING) == [(3,)]
    # identical rerun: nothing ingested, nothing rebuilt, nothing republished
    assert (second["ingest"]["loaded"], second["changes"]["rebuild"]) == (0, False)
    assert second["changes"]["reason"] == "inputs_unchanged"
    assert staged(second["pipeline_run_id"]) == 0
    assert q("SELECT count(*) FROM meta.publications") == [(1,)]
    assert diff(after_first, published()) == []
    assert q(
        "SELECT event FROM meta.ingestion_events WHERE task = 'detect_changes' "
        "AND pipeline_run_id = :r",
        r=second["pipeline_run_id"],
    ) == [("skipped_unchanged",)]


# ------------------------------------------------------------------------------ 3, 4 & 5


def test_new_modified_and_deleted_rows_follow_the_snapshot_policy(tmp_path: Path) -> None:
    run(write_versioned_source(tmp_path / "v1", base_rows(), CONTRACTS))
    result = run(write_versioned_source(tmp_path / "v2", v2_rows(), CONTRACTS))

    assert result["changes"] == {**result["changes"], "rebuild": True, "reason": "inputs_changed"}
    orders = {r[0] for r in q("SELECT order_id FROM warehouse.fct_orders", Role.REPORTING)}
    assert orders == {order(1), order(2), order(4)}  # new in, deleted out
    assert q(
        "SELECT review_score FROM warehouse.fct_reviews WHERE review_id = :r",
        Role.REPORTING,
        r=review(1),
    ) == [(2,)]  # modified: latest snapshot wins
    # raw keeps every snapshot for lineage; src/warehouse use only the latest one
    assert q("SELECT count(*) FROM meta.source_files WHERE source_table = 'order_reviews'") == [
        (2,)
    ]
    assert q("SELECT count(*) FROM raw.order_reviews") == [(3 + 3,)]  # v1: 3 rows, v2: 3 rows


def test_no_duplicates_after_repeated_versions(tmp_path: Path) -> None:
    v1 = write_versioned_source(tmp_path / "v1", base_rows(), CONTRACTS)
    v2 = write_versioned_source(tmp_path / "v2", v2_rows(), CONTRACTS)
    for version in (v1, v2, v2, v1, v2):  # includes reloading an older snapshot
        run(version, force_reload=True)

    for table, key in (
        ("fct_orders", "order_id"),
        ("fct_order_items", "order_id, order_item_id"),
        ("fct_payments", "order_id, payment_sequential"),
        ("fct_reviews", "review_id, order_id"),
        ("dim_customer", "customer_unique_id"),
    ):
        dupes = q(
            f"SELECT count(*) FROM (SELECT {key} FROM warehouse.{table} "
            f"GROUP BY {key} HAVING count(*) > 1) d",
            Role.REPORTING,
        )
        assert dupes == [(0,)], table
    assert q("SELECT count(*) FROM warehouse.fct_orders", Role.REPORTING) == [(3,)]
    # two distinct snapshots per table registered, never more: reloads replace, not append
    assert q("SELECT count(*) FROM meta.source_files WHERE source_table = 'orders'") == [(2,)]


# ------------------------------------------------------------------------------ 6


def test_full_refresh_equals_clean_reconstruction(tmp_path: Path) -> None:
    v1 = write_versioned_source(tmp_path / "v1", base_rows(), CONTRACTS)
    v2 = write_versioned_source(tmp_path / "v2", v2_rows(), CONTRACTS)
    run(v1)
    run(v2)
    refreshed = run(v2, full_refresh=True)
    assert refreshed["changes"]["reason"] == "full_refresh_requested"
    incremental_state = published()

    reset_everything()  # a brand-new warehouse that only ever saw v2
    run(v2)
    assert diff(incremental_state, published()) == []


# ---------------------------------------------------------------- logic changes & rollback


def test_changed_transformation_logic_triggers_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v1 = write_versioned_source(tmp_path / "v1", base_rows(), CONTRACTS)
    run(v1)
    monkeypatch.setattr(change_detection, "transform_fingerprint", lambda root=None: "f" * 64)

    result = run(v1)  # same source files, different dbt/contract/staging logic

    assert (result["changes"]["rebuild"], result["changes"]["reason"]) == (True, "inputs_changed")
    assert staged(result["pipeline_run_id"]) == 9


def test_after_rollback_the_next_run_rebuilds_the_current_inputs(tmp_path: Path) -> None:
    v1 = write_versioned_source(tmp_path / "v1", base_rows(), CONTRACTS)
    v2 = write_versioned_source(tmp_path / "v2", v2_rows(), CONTRACTS)
    run(v1)
    run(v2)
    engine = get_engine(Role.PIPELINE)
    rollback(engine, start_run(engine, "olist_rollback", "test", 2))
    assert q("SELECT count(*) FROM warehouse.fct_orders", Role.REPORTING) == [(3,)]  # v1 again

    result = run(v2)  # the active version is v1, the current inputs are v2

    assert (result["changes"]["rebuild"], result["changes"]["reason"]) == (True, "inputs_changed")
    orders = {r[0] for r in q("SELECT order_id FROM warehouse.fct_orders", Role.REPORTING)}
    assert orders == {order(1), order(2), order(4)}
