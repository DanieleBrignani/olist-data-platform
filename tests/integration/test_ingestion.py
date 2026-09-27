"""file -> Postgres raw layer: idempotency, quarantine, failure bookkeeping.

Uses small SYNTHETIC files in the real contract formats (allowed for controlled tests only;
never used for benchmarks). Real-data coverage is in test_real_ingestion.py.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from sqlalchemy import text

from olist_platform.config import Role
from olist_platform.database.engine import get_engine
from olist_platform.errors import DataContractError, ManifestMismatchError
from olist_platform.ingestion.manifest import Lock, LockEntry, build_manifest
from olist_platform.ingestion.pipeline import run_ingestion
from olist_platform.validation.contracts import Contract, load_contracts

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]

ALL = load_contracts()
SUBSET = {k: ALL[k] for k in ("sellers", "product_category_translation")}
SELLERS_HEADER = '"seller_id","seller_zip_code_prefix","seller_city","seller_state"'


def seller_row(i: int) -> str:
    return f"{i:032x},{10000 + i % 90000:05d},campinas,SP"


def write_sources(
    directory: Path,
    seller_lines: list[str],
    *,
    header: str = SELLERS_HEADER,
    translation: bytes | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "olist_sellers_dataset.csv").write_text(
        "\n".join([header, *seller_lines]) + "\n", encoding="utf-8", newline=""
    )
    (directory / "product_category_name_translation.csv").write_bytes(
        translation
        or b"\xef\xbb\xbfproduct_category_name,product_category_name_english\r\n"
        b"beleza_saude,health_beauty\r\nartes,art"
    )
    return directory


def lock_for(directory: Path, contracts: dict[str, Contract] = SUBSET) -> Lock:
    entries = build_manifest(directory, contracts)
    return Lock(
        dataset="synthetic",
        dataset_version=2,
        files=[LockEntry(**e.model_dump(include=set(LockEntry.model_fields))) for e in entries],
    )


def ingest(directory: Path, lock: Lock, **kwargs):
    return run_ingestion(
        get_engine(Role.PIPELINE), directory, SUBSET, environment="test", lock=lock, **kwargs
    )


def scalar(sql: str, **params):
    with get_engine(Role.ADMIN).connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


def test_load_then_rerun_is_a_recorded_no_op(tmp_path: Path) -> None:
    src = write_sources(tmp_path, [seller_row(i) for i in range(1, 51)])
    lock = lock_for(src)

    first = ingest(src, lock)
    second = ingest(src, lock)

    assert [r.status for r in first.results] == ["loaded", "loaded"]
    assert [r.status for r in second.results] == ["skipped", "skipped"]
    assert scalar("SELECT count(*) FROM raw.sellers") == 50
    assert scalar("SELECT count(*) FROM raw.product_category_translation") == 2
    assert scalar("SELECT count(*) FROM meta.source_files") == 2
    assert (
        scalar("SELECT count(*) FROM meta.ingestion_events WHERE event = 'skipped_already_loaded'")
        == 2
    )
    assert scalar("SELECT count(*) FROM meta.pipeline_runs WHERE status = 'success'") == 2


def test_force_reload_replaces_rows_without_duplicates(tmp_path: Path) -> None:
    src = write_sources(tmp_path, [seller_row(i) for i in range(1, 51)])
    lock = lock_for(src)
    ingest(src, lock)
    first_run_ids = scalar("SELECT count(DISTINCT _pipeline_run_id) FROM raw.sellers")

    summary = ingest(src, lock, force_reload=True)

    assert scalar("SELECT count(*) FROM raw.sellers") == 50
    assert first_run_ids == 1
    assert scalar("SELECT count(DISTINCT _pipeline_run_id) FROM raw.sellers") == 1
    assert scalar("SELECT min(_pipeline_run_id::text) FROM raw.sellers") == str(
        summary.pipeline_run_id
    )


def test_values_are_stored_exactly_as_in_the_file(tmp_path: Path) -> None:
    src = write_sources(tmp_path, [f"{1:032x},01003,  são paulo ,SP", f"{2:032x},01004,,SP"])
    ingest(src, lock_for(src))
    with get_engine(Role.ADMIN).connect() as conn:
        rows = conn.execute(
            text("SELECT seller_zip_code_prefix, seller_city FROM raw.sellers ORDER BY _row_number")
        ).all()
    assert rows == [("01003", "  são paulo "), ("01004", "")]  # zeros, spaces, '' preserved


def test_bom_file_first_column_is_clean(tmp_path: Path) -> None:
    src = write_sources(tmp_path, [seller_row(1)])
    ingest(src, lock_for(src))
    assert (
        scalar(
            "SELECT product_category_name FROM raw.product_category_translation "
            "WHERE _row_number = 1"
        )
        == "beleza_saude"
    )


def test_malformed_record_is_quarantined_not_dropped(tmp_path: Path) -> None:
    lines = [seller_row(i) for i in range(1, 201)]
    lines[41] = f"{42:032x},10042,campinas"  # record 42: one field missing
    src = write_sources(tmp_path, lines)

    summary = ingest(src, lock_for(src))

    sellers = next(r for r in summary.results if r.table == "sellers")
    assert (sellers.rows_read, sellers.rows_loaded, sellers.rows_rejected) == (200, 199, 1)
    assert scalar("SELECT count(*) FROM raw.sellers") == 199
    with get_engine(Role.ADMIN).connect() as conn:
        reject = conn.execute(
            text(
                "SELECT r.record_ref, r.rule_name, r.severity, r.reason, r.raw_record, "
                "r.pipeline_run_id = :run AS same_run FROM meta.rejected_records r"
            ),
            {"run": summary.pipeline_run_id},
        ).one()
    assert reject.record_ref == "olist_sellers_dataset.csv#record=42"
    assert (reject.rule_name, reject.severity) == ("record_structure", "ERROR")
    assert "expected 4 fields, found 3" in reject.reason
    assert reject.raw_record["fields"] == [f"{42:032x}", "10042", "campinas"]
    assert reject.same_run
    assert scalar("SELECT rows_rejected FROM meta.source_files WHERE source_table='sellers'") == 1


def test_reject_ratio_over_limit_fails_file_but_keeps_evidence(tmp_path: Path) -> None:
    lines = [seller_row(i) for i in range(1, 11)]
    lines[0] = "only-one-field"  # 1 of 10 = 10% > 1% limit
    src = write_sources(tmp_path, lines)

    with pytest.raises(DataContractError, match="rejected"):
        ingest(src, lock_for(src))

    assert scalar("SELECT count(*) FROM raw.sellers") == 0  # data transaction rolled back
    assert scalar("SELECT status FROM meta.source_files WHERE source_table='sellers'") == "failed"
    assert scalar("SELECT count(*) FROM meta.rejected_records") == 1
    assert scalar("SELECT count(*) FROM meta.ingestion_events WHERE event='file_rejected'") == 1
    assert scalar("SELECT failed_task || ':' || error_type FROM meta.pipeline_runs") == (
        "ingest_raw:data_contract_violation"
    )


def test_failed_file_can_be_reloaded_after_fix(tmp_path: Path) -> None:
    lines = [seller_row(i) for i in range(1, 11)]
    broken = write_sources(tmp_path / "broken", ["bad", *lines[1:]])
    with pytest.raises(DataContractError):
        ingest(broken, lock_for(broken))

    fixed = write_sources(tmp_path / "fixed", lines)
    ingest(fixed, lock_for(fixed))

    assert scalar("SELECT count(*) FROM raw.sellers") == 10
    assert (
        scalar(
            "SELECT count(*) FROM meta.source_files "
            "WHERE source_table='sellers' AND status='loaded'"
        )
        == 1
    )


def test_breaking_header_stops_before_any_write(tmp_path: Path) -> None:
    src = write_sources(
        tmp_path, [seller_row(1)], header=SELLERS_HEADER.replace("seller_state", "seller_uf")
    )

    with pytest.raises(DataContractError, match="breaking schema change"):
        ingest(src, lock_for(src))

    assert scalar("SELECT count(*) FROM meta.source_files") == 0
    assert scalar("SELECT count(*) FROM raw.product_category_translation") == 0
    assert scalar("SELECT failed_task FROM meta.pipeline_runs") == "validate_source"


def test_file_changed_after_lock_fails_manifest_check(tmp_path: Path) -> None:
    src = write_sources(tmp_path, [seller_row(i) for i in range(1, 6)])
    lock = lock_for(src)
    path = src / "olist_sellers_dataset.csv"
    os.chmod(path, stat.S_IWUSR | stat.S_IRUSR)
    path.write_text(path.read_text().replace("campinas", "campinaz"), newline="")  # same size

    with pytest.raises(ManifestMismatchError, match="sha256"):
        ingest(src, lock)

    assert scalar("SELECT count(*) FROM meta.source_files") == 0
    assert scalar("SELECT failed_task || ':' || error_type FROM meta.pipeline_runs") == (
        "verify_manifest:manifest_mismatch"
    )


def test_unbalanced_quotes_reject_the_whole_file(tmp_path: Path) -> None:
    lines = [seller_row(i) for i in range(1, 11)]
    lines[4] = f'{5:032x},10005,"campinas,SP'  # quote never closed
    src = write_sources(tmp_path, lines)

    with pytest.raises(DataContractError):
        ingest(src, lock_for(src))
    assert scalar("SELECT count(*) FROM raw.sellers") == 0


def test_new_file_version_is_loaded_alongside_with_distinct_lineage(tmp_path: Path) -> None:
    v1 = write_sources(tmp_path / "v1", [seller_row(i) for i in range(1, 6)])
    v2 = write_sources(tmp_path / "v2", [seller_row(i) for i in range(1, 8)])
    ingest(v1, lock_for(v1))
    ingest(v2, lock_for(v2))

    assert scalar("SELECT count(DISTINCT _source_file_id) FROM raw.sellers") == 2
    assert scalar("SELECT count(*) FROM raw.sellers") == 12  # history kept per file
    assert (
        scalar(
            "SELECT count(*) FROM (SELECT _source_file_id, _row_number FROM raw.sellers "
            "GROUP BY 1, 2 HAVING count(*) > 1) d"
        )
        == 0
    )
