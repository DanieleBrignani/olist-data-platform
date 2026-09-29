"""The real `olist_refresh` flow, end to end, on a Prefect engine (ephemeral test server).

Synthetic, contract-shaped files -> raw -> src -> dbt -> quality gate -> published marts.
Also proves orchestration-level failure handling: a quality-gate failure stops the flow at
`quality_gate`, is not retried, and leaves the published marts untouched.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from prefect.testing.utilities import prefect_test_harness
from sqlalchemy import text

from olist_platform.config import Role
from olist_platform.database.engine import get_engine
from olist_platform.database.fingerprint import diff, fingerprint
from olist_platform.errors import ManifestMismatchError, QualityGateError
from olist_platform.ingestion.manifest import build_manifest, write_lock
from olist_platform.validation.contracts import load_contracts
from orchestration.olist_flow import olist_refresh
from tests.synthetic import base_rows, order, prod, seller, write_dataset

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.usefixtures("clean_db")]

CONTRACTS = load_contracts()


@pytest.fixture(scope="module", autouse=True)
def prefect_backend() -> Iterator[None]:
    with prefect_test_harness():
        yield


@pytest.fixture(autouse=True)
def _dbt_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DBT_TARGET_PATH", str(tmp_path / "dbt_target"))


def dataset(tmp_path: Path, rows: dict, name: str) -> tuple[str, str]:
    """Write files where the flow expects them (<root>/raw/olist/v2) plus a matching lock."""
    root = tmp_path / name
    directory = write_dataset(root / "raw" / "olist" / "v2", rows, CONTRACTS)
    lock = write_lock(
        build_manifest(directory, CONTRACTS), "synthetic", 2, path=root / "manifest.lock.json"
    )
    return str(root), str(lock)


def q(sql: str, role: Role = Role.PIPELINE):
    with get_engine(role).connect() as conn:
        return conn.execute(text(sql)).all()


def task_events(run_id: str) -> list[str]:
    return [
        r[0]
        for r in q(
            "SELECT DISTINCT ON (task) task FROM meta.ingestion_events "
            f"WHERE pipeline_run_id = '{run_id}' ORDER BY task"
        )
    ]


def test_flow_runs_all_steps_and_publishes(tmp_path: Path) -> None:
    root, lock = dataset(tmp_path, base_rows(), "ok")

    summary = olist_refresh(data_root=root, lock_path=lock)

    assert summary["ingest"]["loaded"] == 9
    assert summary["quality_gate"]["tests"] > 30
    assert summary["published_tables"] == 16
    assert summary["metrics"]["rows_rejected"] == 0
    run = q(
        "SELECT status, flow_name, failed_task FROM meta.pipeline_runs "
        f"WHERE pipeline_run_id = '{summary['pipeline_run_id']}'"
    )
    assert run == [("success", "olist_refresh", None)]
    assert q("SELECT sum(orders) FROM marts.mart_sales", Role.REPORTING) == [(3,)]
    assert set(task_events(summary["pipeline_run_id"])) == {
        "ingest_raw",
        "detect_changes",
        "load_staging",
    }


def test_gate_failure_stops_flow_at_quality_gate_and_keeps_published_data(
    tmp_path: Path,
) -> None:
    root, lock = dataset(tmp_path, base_rows(), "good")
    olist_refresh(data_root=root, lock_path=lock)
    before = q("SELECT count(*) FROM warehouse.fct_order_items", Role.REPORTING)

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
    root_bad, lock_bad = dataset(tmp_path, bad, "bad")
    with pytest.raises(QualityGateError):
        olist_refresh(data_root=root_bad, lock_path=lock_bad)

    assert q("SELECT count(*) FROM warehouse.fct_order_items", Role.REPORTING) == before
    assert q(
        "SELECT status, failed_task, error_type FROM meta.pipeline_runs WHERE status = 'failed'"
    ) == [("failed", "quality_gate", "quality_gate_failed")]
    # deterministic failure: the gate evaluated exactly once (no retry)
    assert q("SELECT count(*) FROM meta.quality_gate_decisions WHERE decision = 'FAIL'") == [(1,)]


def published() -> dict:
    with get_engine(Role.REPORTING).connect() as conn:
        return fingerprint(conn)


def test_idempotency_running_the_flow_twice_gives_identical_published_data(
    tmp_path: Path,
) -> None:
    root, lock = dataset(tmp_path, base_rows(), "idem")

    first = olist_refresh(data_root=root, lock_path=lock)
    after_first = published()
    # full_refresh: really rebuild and republish from identical inputs (a plain rerun would
    # be skipped as unchanged; that path is covered in test_incremental.py)
    second = olist_refresh(data_root=root, lock_path=lock, full_refresh=True)
    after_second = published()

    assert second["changes"]["rebuild"] is True
    assert diff(after_first, after_second) == []  # every table: same rows, same content
    assert len(after_first) == 16
    assert (first["ingest"]["loaded"], second["ingest"]["loaded"]) == (9, 0)
    assert second["ingest"]["skipped"] == 9  # same checksums -> recorded no-op
    assert q("SELECT count(*) FROM raw.orders") == [(3,)]  # no uncontrolled duplicates
    assert q("SELECT count(*) FROM meta.publications WHERE action = 'publish'") == [(2,)]


def test_tampered_source_fails_at_verify_manifest_before_any_write(tmp_path: Path) -> None:
    root, lock = dataset(tmp_path, base_rows(), "tampered")
    sellers = Path(root) / "raw" / "olist" / "v2" / "olist_sellers_dataset.csv"
    sellers.write_text(sellers.read_text().replace("campinas", "campinaz"), newline="")

    with pytest.raises(ManifestMismatchError):
        olist_refresh(data_root=root, lock_path=lock)

    assert q("SELECT failed_task, error_type FROM meta.pipeline_runs WHERE status = 'failed'") == [
        ("verify_manifest", "manifest_mismatch")
    ]
    assert q("SELECT count(*) FROM meta.source_files") == [(0,)]  # nothing ingested
