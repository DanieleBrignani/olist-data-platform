"""REAL DATA, end to end: the 9 Olist v2 files -> published warehouse and marts, via the real
Prefect flow, run THREE times (initial load, plain rerun, forced reload of every file).

Proves on the real dataset that:
  * raw files -> final marts works end to end, with every locked record accounted for;
  * the pipeline is idempotent: all 16 published tables are identical (row count AND content
    md5) after every run, even when every file is re-copied from scratch;
  * reruns never create uncontrolled duplicates.

Runs against the isolated olist_dw_test database. Slow (three full runs): marked e2e +
source_data, and skipped when the dataset has not been fetched.
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
from olist_platform.ingestion.manifest import load_lock
from olist_platform.validation.contracts import load_contracts
from orchestration.olist_flow import olist_refresh

pytestmark = [pytest.mark.e2e, pytest.mark.source_data, pytest.mark.integration]

DATA_ROOT = Path(__file__).resolve().parents[2] / "data"
LOCK = load_lock()
TOTAL_LOCKED_RECORDS = sum(e.row_count for e in LOCK.files)


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> Iterator[list[dict]]:
    if not (DATA_ROOT / "raw" / "olist" / "v2" / "olist_orders_dataset.csv").is_file():
        pytest.skip("dataset not present; run `olist source fetch`")
    mp = pytest.MonkeyPatch()
    mp.setenv("DBT_TARGET_PATH", str(tmp_path_factory.mktemp("dbt_real")))
    with get_engine(Role.ADMIN).begin() as conn:  # start from an empty test warehouse
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

    results = []
    with prefect_test_harness():
        for force_reload in (False, False, True):
            summary = olist_refresh(data_root=str(DATA_ROOT), force_reload=force_reload)
            with get_engine(Role.REPORTING).connect() as conn:
                summary["fingerprint"] = fingerprint(conn)
            results.append(summary)
    mp.undo()
    yield results


def q(sql: str, role: Role = Role.PIPELINE):
    with get_engine(role).connect() as conn:
        return conn.execute(text(sql)).all()


def test_raw_files_to_marts_accounts_for_every_record(runs: list[dict]) -> None:
    first = runs[0]
    assert first["ingest"]["loaded"] == 9
    assert first["ingest"]["rows_loaded"] + first["ingest"]["rows_rejected"] == (
        TOTAL_LOCKED_RECORDS
    )
    assert first["quality_gate"]["warnings"] == 6
    assert first["published_tables"] == 16
    # business totals in the published marts reconcile with the source files
    orders = LOCK.entry("olist_orders_dataset.csv").row_count
    items = LOCK.entry("olist_order_items_dataset.csv").row_count
    assert q("SELECT sum(orders) FROM marts.mart_sales", Role.REPORTING) == [(orders,)]
    assert q("SELECT count(*) FROM warehouse.fct_order_items", Role.REPORTING) == [(items,)]


def test_every_run_publishes_byte_identical_tables(runs: list[dict]) -> None:
    baseline = runs[0]["fingerprint"]
    assert len(baseline) == 16
    for label, run in zip(("plain rerun", "forced reload"), runs[1:], strict=True):
        assert diff(baseline, run["fingerprint"]) == [], label


def test_reruns_skip_or_replace_but_never_duplicate(runs: list[dict]) -> None:
    assert runs[1]["ingest"] == {"loaded": 0, "skipped": 9, "rows_loaded": 0, "rows_rejected": 0}
    assert runs[2]["ingest"]["loaded"] == 9  # forced: every file re-copied...
    for contract in load_contracts().values():  # ...yet raw holds exactly one copy
        expected = LOCK.entry(contract.file).row_count
        assert q(f"SELECT count(*) FROM raw.{contract.table}") == [(expected,)], contract.table
    assert q("SELECT count(*) FROM meta.source_files") == [(9,)]
    assert q("SELECT status, count(*) FROM meta.pipeline_runs GROUP BY 1") == [("success", 3)]
