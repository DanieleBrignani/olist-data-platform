"""Real Olist v2 files -> raw -> src: every locked record is accounted for, reruns are safe.

The dataset is pinned by sha256 (manifest.lock.json), so the staging outcome below is a
deterministic, measured fact about v2 — a regression guard for the rule engine.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from olist_platform.config import Role
from olist_platform.database.engine import get_engine
from olist_platform.ingestion.manifest import load_lock
from olist_platform.ingestion.pipeline import run_ingestion, run_staging
from olist_platform.ingestion.source import raw_dir
from olist_platform.validation.contracts import load_contracts

pytestmark = [pytest.mark.integration, pytest.mark.source_data, pytest.mark.usefixtures("clean_db")]

DATA_DIR = raw_dir(Path(__file__).resolve().parents[2] / "data")


def test_real_files_load_completely_and_rerun_is_noop() -> None:
    if not (DATA_DIR / "olist_orders_dataset.csv").is_file():
        pytest.skip("dataset not present; run `olist source fetch`")
    contracts, lock = load_contracts(), load_lock()
    engine = get_engine(Role.PIPELINE)

    first = run_ingestion(engine, DATA_DIR, contracts, environment="test")
    second = run_ingestion(engine, DATA_DIR, contracts, environment="test")

    assert {r.status for r in second.results} == {"skipped"}
    with get_engine(Role.ADMIN).connect() as conn:
        for contract in contracts.values():
            locked = lock.entry(contract.file).row_count
            count_sql = f"SELECT count(*) FROM raw.{contract.table}"
            loaded = conn.execute(text(count_sql)).scalar_one()
            rejected = conn.execute(
                text(
                    "SELECT count(*) FROM meta.rejected_records "
                    "WHERE source_table = :t AND layer = 'raw'"
                ),
                {"t": contract.table},
            ).scalar_one()
            assert loaded + rejected == locked, contract.table
    assert first.rows_loaded + first.rows_rejected == sum(e.row_count for e in lock.files)


# Measured on Olist v2 (first `olist stage` run, 2026-09-26); cross-checked against the
# independent profiler in docs/source_profile.md.
EXPECTED_STAGING = {
    # table: (src rows, flagged records, exact duplicates collapsed)
    "geolocation": (738_332, 31, 261_831),
    "product_category_translation": (71, 0, 0),
    "customers": (99_441, 278, 0),
    "products": (32_951, 17, 0),
    "sellers": (3_095, 8, 0),
    "orders": (99_441, 203, 0),
    "order_items": (112_650, 0, 0),
    "order_payments": (103_886, 5, 0),
    "order_reviews": (99_224, 0, 0),
}


def test_real_staging_outcome_is_exactly_the_measured_one() -> None:
    if not (DATA_DIR / "olist_orders_dataset.csv").is_file():
        pytest.skip("dataset not present; run `olist source fetch`")
    contracts = load_contracts()
    engine = get_engine(Role.PIPELINE)
    run_ingestion(engine, DATA_DIR, contracts, environment="test")

    results = {r.table: r for r in run_staging(engine, contracts, environment="test")}

    observed = {t: (r.src_rows, r.flagged, r.duplicates_collapsed) for t, r in results.items()}
    assert observed == EXPECTED_STAGING
    assert all(r.quarantined == 0 for r in results.values())
    for r in results.values():  # reconciliation: raw = src rows + collapsed + quarantined
        assert r.raw_rows == r.src_rows + r.duplicates_collapsed + r.quarantined, r.table
