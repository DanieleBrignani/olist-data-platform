from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from olist_platform.errors import ManifestMismatchError
from olist_platform.ingestion.loader import iter_records, structural_problem
from olist_platform.ingestion.manifest import (
    Lock,
    LockEntry,
    build_manifest,
    count_records,
    load_lock,
    verify_against_lock,
    write_manifest,
)
from olist_platform.validation.contracts import CONTRACTS_DIR, load_contracts

CONTRACTS = load_contracts()
REVIEWS = CONTRACTS["order_reviews"]
REVIEW_HEADER = ",".join(f'"{c}"' for c in REVIEWS.column_names)
ROOT = CONTRACTS_DIR.parent


def _reviews_file(tmp_path: Path) -> Path:
    body = (
        f"{REVIEW_HEADER}\r\n"
        f'{"a" * 32},{"b" * 32},5,,"linha um\r\nlinha dois",'
        "2018-01-01 00:00:00,2018-01-02 10:00:00\r\n"
        f"{'c' * 32},{'d' * 32},1,titulo,,2018-01-01 00:00:00,2018-01-03 10:00:00\r\n"
    )
    path = tmp_path / REVIEWS.file
    path.write_bytes(body.encode())
    return path


def test_multiline_quoted_field_counts_as_one_record(tmp_path: Path) -> None:
    path = _reviews_file(tmp_path)
    assert count_records(path, REVIEWS) == 2
    records = list(iter_records(path, REVIEWS))
    assert [n for n, _, _ in records] == [1, 2]
    assert records[0][1][4] == "linha um\r\nlinha dois"


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (["a", "b", "c"], None),
        (["a", "b"], "expected 3 fields, found 2"),
        (["a", "b", "c", "d"], "expected 3 fields, found 4"),
        (["a", "b" + chr(0), "c"], "NUL byte"),
    ],
)
def test_structural_problem(record: list[str], expected: str | None) -> None:
    problem = structural_problem(record, 3)
    if expected is None:
        assert problem is None
    else:
        assert problem is not None and expected in problem[1]


def _lock_for(path: Path, **overrides) -> Lock:
    entry = build_manifest(path.parent, {"order_reviews": REVIEWS})[0]
    fields = entry.model_dump(include=set(LockEntry.model_fields)) | overrides
    return Lock(dataset="t", dataset_version=2, files=[LockEntry(**fields)])


def test_verify_passes_and_returns_checksums(tmp_path: Path) -> None:
    path = _reviews_file(tmp_path)
    lock = _lock_for(path)
    assert verify_against_lock(tmp_path, lock) == {REVIEWS.file: lock.files[0].sha256}


@pytest.mark.parametrize(
    ("override", "message"),
    [({"size": 1}, "size"), ({"sha256": "0" * 64}, "sha256")],
)
def test_verify_detects_changes(tmp_path: Path, override: dict, message: str) -> None:
    path = _reviews_file(tmp_path)
    with pytest.raises(ManifestMismatchError, match=message):
        verify_against_lock(tmp_path, _lock_for(path, **override))


def test_verify_detects_missing_file(tmp_path: Path) -> None:
    lock = _lock_for(_reviews_file(tmp_path))
    (tmp_path / REVIEWS.file).unlink()
    with pytest.raises(ManifestMismatchError, match="missing"):
        verify_against_lock(tmp_path, lock)


def test_manifest_has_required_fields_and_is_written_once(tmp_path: Path) -> None:
    _reviews_file(tmp_path)
    entries = build_manifest(tmp_path, {"order_reviews": REVIEWS})
    assert set(entries[0].model_dump()) == {
        "filename",
        "size",
        "sha256",
        "row_count",
        "ingestion_timestamp",
        "dataset_version",
    }
    write_manifest(tmp_path, entries)
    with pytest.raises(FileExistsError):
        write_manifest(tmp_path, entries)


def test_committed_lock_covers_every_contract_file() -> None:
    lock = load_lock()
    assert {e.filename for e in lock.files} == {c.file for c in CONTRACTS.values()}
    assert lock.dataset == "olistbr/brazilian-ecommerce"
    assert lock.dataset_version == 2


def test_raw_migration_columns_match_contracts() -> None:
    spec = importlib.util.spec_from_file_location(
        "m0002", ROOT / "migrations" / "versions" / "0002_meta_and_raw_tables.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert {t: c.column_names for t, c in CONTRACTS.items()} == module.RAW_TABLES
