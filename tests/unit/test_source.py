from __future__ import annotations

import stat
import zipfile
from pathlib import Path

import pytest

from olist_platform.errors import DeterministicError, TransientError
from olist_platform.ingestion.source import locate_or_download, raw_dir, sha256_file

FILES = ["a.csv", "b.csv"]


def _archive(tmp_path: Path, members: dict[str, str]) -> str:
    path = tmp_path / "archive.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return path.as_uri()


def test_downloads_extracts_and_makes_files_read_only(tmp_path: Path) -> None:
    url = _archive(tmp_path, {"a.csv": "x\n1\n", "b.csv": "y\n2\n"})
    location = locate_or_download(tmp_path / "data", FILES, url=url)

    assert location.action == "downloaded"
    assert [p.name for p in location.files] == FILES
    for path in location.files:
        assert not path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    assert (location.directory / "a.csv").read_text() == "x\n1\n"


def test_second_call_locates_without_downloading(tmp_path: Path) -> None:
    url = _archive(tmp_path, {"a.csv": "x\n", "b.csv": "y\n"})
    first = locate_or_download(tmp_path / "data", FILES, url=url)
    before = {p.name: sha256_file(p) for p in first.files}

    second = locate_or_download(tmp_path / "data", FILES, url="file:///does/not/exist.zip")

    assert second.action == "located"
    assert {p.name: sha256_file(p) for p in second.files} == before


def test_partially_populated_raw_dir_is_never_overwritten(tmp_path: Path) -> None:
    target = raw_dir(tmp_path / "data")
    target.mkdir(parents=True)
    (target / "a.csv").write_text("existing")
    with pytest.raises(DeterministicError, match="partially populated"):
        locate_or_download(tmp_path / "data", FILES, url=_archive(tmp_path, {}))
    assert (target / "a.csv").read_text() == "existing"


def test_archive_missing_expected_file_is_deterministic(tmp_path: Path) -> None:
    url = _archive(tmp_path, {"a.csv": "x\n"})
    with pytest.raises(DeterministicError, match=r"missing expected files: \['b.csv'\]"):
        locate_or_download(tmp_path / "data", FILES, url=url)
    assert not any(raw_dir(tmp_path / "data").iterdir())


def test_corrupt_archive_is_transient(tmp_path: Path) -> None:
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(TransientError, match="corrupt"):
        locate_or_download(tmp_path / "data", FILES, url=bad.as_uri())


def test_unreachable_source_is_transient(tmp_path: Path) -> None:
    with pytest.raises(TransientError):
        locate_or_download(tmp_path / "data", FILES, url="http://127.0.0.1:9/none.zip", timeout_s=2)
