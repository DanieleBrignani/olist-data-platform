"""Failure recovery on the real flow (docs/failure-recovery.md).

Guarantee under test: whatever fails, consumers keep seeing the last complete, gated version;
deterministic failures are not retried; the next run recovers without manual cleanup.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from prefect.testing.utilities import prefect_test_harness
from sqlalchemy import create_engine, text

from olist_platform.config import Role, get_settings
from olist_platform.database.engine import get_engine
from olist_platform.database.fingerprint import diff, fingerprint
from olist_platform.errors import QualityGateError, TransientError
from olist_platform.ingestion.loader import ingest_file
from olist_platform.ingestion.manifest import build_manifest
from olist_platform.ingestion.source import raw_dir
from olist_platform.transform.dbt_runner import DbtError
from olist_platform.validation.contracts import load_contracts
from orchestration import olist_flow
from tests.synthetic import base_rows, order, prod, seller, write_versioned_source

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.usefixtures("clean_db")]

CONTRACTS = load_contracts()
CORE_TABLES = 9  # 5 dimensions + 4 facts


@pytest.fixture(scope="module", autouse=True)
def prefect_backend() -> Iterator[None]:
    with prefect_test_harness():
        yield


@pytest.fixture(autouse=True)
def _dbt_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DBT_TARGET_PATH", str(tmp_path / "dbt_target"))


def q(sql: str, role: Role = Role.PIPELINE, **params):
    with get_engine(role).connect() as conn:
        return conn.execute(text(sql), params).all()


def published() -> dict:
    with get_engine(Role.REPORTING).connect() as conn:
        return fingerprint(conn)


def run(root_lock: tuple[str, str], **kw) -> dict:
    return olist_flow.olist_refresh(data_root=root_lock[0], lock_path=root_lock[1], **kw)


def with_extra_order(n: int) -> dict:
    rows = base_rows()
    rows["order_items"].append(
        {
            "order_id": order(1),
            "order_item_id": str(n),
            "product_id": prod(2),
            "seller_id": seller(1),
            "shipping_limit_date": "2018-01-10 00:00:00",
            "price": "10.00",
            "freight_value": "1.00",
        }
    )
    return rows


def test_postgres_unavailable_is_a_transient_failure(tmp_path: Path) -> None:
    """(1) DB down mid-pipeline: classified transient, so the task's bounded retry applies."""
    root, _ = write_versioned_source(tmp_path / "v1", base_rows(), CONTRACTS)
    directory = raw_dir(Path(root))
    contract = CONTRACTS["sellers"]
    entry = build_manifest(directory, {"sellers": contract})[0]
    dead = create_engine(
        get_settings().database_url(Role.PIPELINE).set(port=1), connect_args={"connect_timeout": 2}
    )
    with pytest.raises(TransientError, match="database unavailable"):
        ingest_file(dead, uuid.uuid4(), contract, directory / contract.file, entry, entry.sha256)


def test_dbt_model_failure_publishes_nothing_and_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(7) A model error is deterministic: one attempt, fails at dbt_build, old version stays."""
    run(write_versioned_source(tmp_path / "v1", base_rows(), CONTRACTS))
    before = published()
    calls = {"n": 0}

    def broken_build(*_: object, **__: object) -> None:
        calls["n"] += 1
        raise DbtError("dbt run failed: [model.olist.fct_orders]")

    monkeypatch.setattr(olist_flow, "build_models", broken_build)
    with pytest.raises(DbtError):
        run(write_versioned_source(tmp_path / "v2", with_extra_order(2), CONTRACTS))

    assert calls["n"] == 1  # task has retries=1, but only for transient errors
    assert q("SELECT failed_task, error_type FROM meta.pipeline_runs WHERE status = 'failed'") == [
        ("dbt_build", "dbt_failed")
    ]
    assert diff(before, published()) == []


def test_killed_run_is_closed_and_leftovers_are_never_published(tmp_path: Path) -> None:
    """(8) A run killed mid-build leaves a 'running' row and a half-built warehouse_build.
    The next run closes the abandoned row and publishes exactly the current models."""
    v1 = write_versioned_source(tmp_path / "v1", base_rows(), CONTRACTS)
    run(v1)
    killed = uuid.uuid4()
    with get_engine(Role.PIPELINE).begin() as conn:
        conn.execute(
            text(
                "INSERT INTO meta.pipeline_runs (pipeline_run_id, flow_name, environment, "
                "dataset_version, status, started_at) VALUES "
                "(:id, 'olist_refresh', 'test', 2, 'running', now() - interval '4 hours')"
            ),
            {"id": killed},
        )
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS warehouse_build"))
        conn.execute(text("CREATE TABLE warehouse_build.leftover_from_killed_run (x int)"))

    result = run(write_versioned_source(tmp_path / "v2", with_extra_order(2), CONTRACTS))

    assert result["changes"]["rebuild"] is True
    assert q(
        "SELECT status, error_type FROM meta.pipeline_runs WHERE pipeline_run_id = :r", r=killed
    ) == [("failed", "abandoned")]
    tables = {
        t
        for (t,) in q(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'warehouse'",
            Role.REPORTING,
        )
    }
    assert "leftover_from_killed_run" not in tables
    assert len(tables) == CORE_TABLES


def test_recent_running_rows_are_not_closed() -> None:
    with get_engine(Role.PIPELINE).begin() as conn:
        conn.execute(
            text(
                "INSERT INTO meta.pipeline_runs (pipeline_run_id, flow_name, environment, "
                "dataset_version, status) VALUES (:id, 'olist_refresh', 'test', 2, 'running')"
            ),
            {"id": uuid.uuid4()},
        )
    from olist_platform.ingestion.runs import start_run  # noqa: PLC0415

    start_run(get_engine(Role.PIPELINE), "probe", "test", 2)
    assert q(
        "SELECT count(*) FROM meta.pipeline_runs WHERE flow_name = 'olist_refresh' "
        "AND status = 'running'"
    ) == [(1,)]


def test_second_run_after_a_failed_run_publishes_the_fixed_data(tmp_path: Path) -> None:
    """(9) A gate failure leaves no publication for its inputs, so fixed data is rebuilt."""
    run(write_versioned_source(tmp_path / "v1", base_rows(), CONTRACTS))
    bad = base_rows()
    bad["order_items"].append(
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
    with pytest.raises(QualityGateError):
        run(write_versioned_source(tmp_path / "bad", bad, CONTRACTS))

    fixed = run(write_versioned_source(tmp_path / "fixed", with_extra_order(2), CONTRACTS))

    assert (fixed["changes"]["rebuild"], fixed["published_tables"]) == (True, 16)
    assert q("SELECT count(*) FROM warehouse.fct_order_items", Role.REPORTING) == [(4,)]
