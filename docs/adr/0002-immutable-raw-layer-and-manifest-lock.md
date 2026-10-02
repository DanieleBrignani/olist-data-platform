# ADR-0002: Immutable raw layer pinned by a committed manifest lock

**Status:** Accepted (2026-09-26)

## Context
The source is the Kaggle dataset `olistbr/brazilian-ecommerce`, **version 2**, last updated
2021-10-01T19:08:27Z, licence **CC BY-NC-SA 4.0**. These facts were verified through the Kaggle
public API on 2026-09-26. The dataset downloads anonymously from
`https://www.kaggle.com/api/v1/datasets/download/olistbr/brazilian-ecommerce?datasetVersionNumber=2`.
The raw files total about 126 MB and carry a non-commercial, share-alike licence, so they are
not committed to Git. Without the files in Git, reproducibility needs a different anchor.

## Decision
1. Files are stored under `data/raw/olist/v2/` exactly as they come out of the archive, and are
   set read-only after extraction. Code never opens them in write mode.
2. After the first download, `scripts/build_manifest.py` writes
   `data/raw/olist/v2/manifest.json` with filename, size, sha256, row_count,
   ingestion_timestamp and dataset_version.
3. The `(filename, size, sha256, row_count)` subset is committed as
   `data_contracts/manifest.lock.json`. Every pipeline run recomputes the checksums. The run
   fails as CRITICAL, without retries, if any file differs from the lock.
4. Moving to a new dataset version is an explicit change: a new `v3/` directory and a new lock,
   reviewed in a PR.

## Consequences
+ Any developer who downloads the data can prove they have the exact bytes behind the
  published benchmark.
+ Tampering and partial downloads are caught before any database write.
− The first run needs network access to Kaggle. Mitigation: `--source-dir` points at a local
  copy, which is verified against the same lock.
− If Kaggle ever changed the v2 archive, the lock would fail loudly, which is the intended
  behaviour. This is not expected, because Kaggle dataset versions are immutable.

## Alternatives rejected
* Committing the CSVs with Git LFS: this adds share-alike licence obligations and repository
  weight for no gain.
* Trusting file names and sizes only: this does not detect content changes.
