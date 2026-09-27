"""Raw-layer manifest and committed lock (ADR-0002).

* manifest.json (next to the raw files, written once): filename, size, sha256, row_count,
  ingestion_timestamp, dataset_version for every file.
* data_contracts/manifest.lock.json (committed): the reproducibility anchor. Every run
  recomputes size + sha256 and fails with ManifestMismatchError on any difference.

row_count is the number of CSV *records* (multi-line quoted fields count once), excluding
the header; the loader reconciles loaded + rejected against it.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from olist_platform.errors import ManifestMismatchError
from olist_platform.ingestion.source import make_read_only, sha256_file
from olist_platform.utils.logging import get_logger
from olist_platform.validation.contracts import CONTRACTS_DIR, Contract

LOCK_PATH = CONTRACTS_DIR / "manifest.lock.json"
MANIFEST_NAME = "manifest.json"

log = get_logger(__name__)


class LockEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    filename: str
    size: int
    sha256: str
    row_count: int


class ManifestEntry(LockEntry):
    ingestion_timestamp: str
    dataset_version: int


class Lock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset: str
    dataset_version: int
    files: list[LockEntry]

    def entry(self, filename: str) -> LockEntry:
        for item in self.files:
            if item.filename == filename:
                return item
        raise ManifestMismatchError(f"{filename} is not in the manifest lock")


def count_records(path: Path, contract: Contract) -> int:
    with path.open(encoding=contract.format.encoding, newline="") as fh:
        reader = csv.reader(
            fh, delimiter=contract.format.delimiter, quotechar=contract.format.quotechar
        )
        next(reader, None)
        return sum(1 for _ in reader)


def build_manifest(directory: Path, contracts: dict[str, Contract]) -> list[ManifestEntry]:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    entries = []
    for contract in sorted(contracts.values(), key=lambda c: c.file):
        path = directory / contract.file
        entries.append(
            ManifestEntry(
                filename=contract.file,
                size=path.stat().st_size,
                sha256=sha256_file(path),
                row_count=count_records(path, contract),
                ingestion_timestamp=now,
                dataset_version=contract.dataset_version,
            )
        )
    return entries


def write_manifest(directory: Path, entries: list[ManifestEntry]) -> Path:
    """Write manifest.json once; an existing manifest is part of the immutable layer."""
    path = directory / MANIFEST_NAME
    if path.exists():
        raise FileExistsError(f"{path} already exists and is immutable")
    path.write_text(
        json.dumps([e.model_dump() for e in entries], indent=2) + "\n", encoding="utf-8"
    )
    make_read_only(path)
    return path


def write_lock(
    entries: list[ManifestEntry], dataset: str, version: int, path: Path = LOCK_PATH
) -> Path:
    lock = Lock(
        dataset=dataset,
        dataset_version=version,
        files=[LockEntry(**e.model_dump(include=set(LockEntry.model_fields))) for e in entries],
    )
    path.write_text(lock.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def load_lock(path: Path = LOCK_PATH) -> Lock:
    return Lock.model_validate_json(path.read_text(encoding="utf-8"))


def verify_against_lock(directory: Path, lock: Lock) -> dict[str, str]:
    """Recompute size + sha256 for every locked file. Returns {filename: sha256}.

    Size is checked first (cheap) so a truncated file fails fast; any difference raises
    ManifestMismatchError, which the orchestrator never retries.
    """
    problems: list[str] = []
    checksums: dict[str, str] = {}
    for entry in lock.files:
        path = directory / entry.filename
        if not path.is_file():
            problems.append(f"{entry.filename}: missing")
            continue
        size = path.stat().st_size
        if size != entry.size:
            problems.append(f"{entry.filename}: size {size} != locked {entry.size}")
            continue
        digest = sha256_file(path)
        if digest != entry.sha256:
            problems.append(
                f"{entry.filename}: sha256 {digest[:12]}… != locked {entry.sha256[:12]}…"
            )
            continue
        checksums[entry.filename] = digest
    if problems:
        for problem in problems:
            log.error("manifest_mismatch", detail=problem, severity="CRITICAL")
        raise ManifestMismatchError("; ".join(problems))
    log.info("manifest_verified", files=len(checksums), dataset_version=lock.dataset_version)
    return checksums
