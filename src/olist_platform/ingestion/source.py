"""Locate or download the Olist source files into the immutable raw layer (ADR-0002).

* If every expected file is already present, nothing is downloaded (`located`).
* Otherwise the pinned dataset version is downloaded from Kaggle's public API, extracted to
  `data/raw/olist/v<version>/`, and every extracted file is made read-only.
* Network failures raise TransientError (retried by the orchestrator); a corrupt archive or a
  missing expected file raises a DeterministicError (never retried).
"""

from __future__ import annotations

import hashlib
import shutil
import stat
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from olist_platform.errors import DeterministicError, TransientError
from olist_platform.utils.logging import get_logger

KAGGLE_DATASET = "olistbr/brazilian-ecommerce"
DATASET_VERSION = 2
DOWNLOAD_URL = (
    "https://www.kaggle.com/api/v1/datasets/download/"
    f"{KAGGLE_DATASET}?datasetVersionNumber={DATASET_VERSION}"
)
CHUNK = 1024 * 1024

log = get_logger(__name__)


@dataclass(frozen=True)
class SourceLocation:
    directory: Path
    files: tuple[Path, ...]
    action: str  # "located" | "downloaded"
    archive_sha256: str | None = None


def raw_dir(data_root: Path, version: int = DATASET_VERSION) -> Path:
    return data_root / "raw" / "olist" / f"v{version}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def make_read_only(path: Path) -> None:
    path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def _download(url: str, target: Path, timeout_s: int) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "olist-platform/0.1"})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as resp, target.open("wb") as out:  # noqa: S310
            shutil.copyfileobj(resp, out, CHUNK)
    except urllib.error.HTTPError as exc:
        # 5xx and 429 are worth retrying; 4xx (e.g. dataset/version removed) is not
        if exc.code >= 500 or exc.code == 429:  # noqa: PLR2004
            raise TransientError(f"HTTP {exc.code} downloading {url}") from exc
        raise DeterministicError(f"HTTP {exc.code} downloading {url}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise TransientError(f"network error downloading {url}: {exc}") from exc


def locate_or_download(
    data_root: Path,
    expected_files: list[str],
    *,
    url: str = DOWNLOAD_URL,
    timeout_s: int = 300,
) -> SourceLocation:
    target_dir = raw_dir(data_root)
    present = [target_dir / name for name in expected_files if (target_dir / name).is_file()]
    if len(present) == len(expected_files):
        log.info("source_located", directory=str(target_dir), files=len(present))
        return SourceLocation(target_dir, tuple(present), "located")

    if present:
        # Never overwrite part of an immutable layer; a human must decide what happened.
        raise DeterministicError(
            f"{target_dir} is partially populated ({len(present)}/{len(expected_files)} files); "
            "refusing to overwrite raw data. Remove the directory to re-download."
        )

    target_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target_dir.parent) as tmp:
        archive = Path(tmp) / "archive.zip"
        log.info("source_download_started", url=url)
        _download(url, archive, timeout_s)
        archive_sha = sha256_file(archive)
        try:
            with zipfile.ZipFile(archive) as zf:
                names = set(zf.namelist())
                missing = sorted(set(expected_files) - names)
                if missing:
                    raise DeterministicError(f"archive is missing expected files: {missing}")
                unexpected = sorted(names - set(expected_files))
                if unexpected:
                    log.warning("source_unexpected_files_ignored", files=unexpected)
                staging = Path(tmp) / "extracted"
                for name in expected_files:
                    zf.extract(name, staging)
        except zipfile.BadZipFile as exc:
            raise TransientError("downloaded archive is corrupt (truncated download?)") from exc

        # Extract fully into a temp dir first, then move: a crash mid-extraction never leaves
        # a partially populated raw directory behind.
        for name in expected_files:
            extracted = staging / name
            make_read_only(extracted)
            extracted.replace(target_dir / name)

    files = tuple(target_dir / name for name in expected_files)
    log.info(
        "source_downloaded",
        directory=str(target_dir),
        files=len(files),
        archive_sha256=archive_sha,
    )
    return SourceLocation(target_dir, files, "downloaded", archive_sha)
