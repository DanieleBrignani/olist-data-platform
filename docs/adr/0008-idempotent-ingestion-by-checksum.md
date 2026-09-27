# ADR-0008: Idempotent ingestion keyed by file checksum

**Status:** Accepted (2026-09-26)

## Context
Reruns must not create uncontrolled duplicates, and partial failures must not leave tables
half-loaded.

## Decision
* `meta.source_files` has `UNIQUE (source_table, sha256)`.
* Loading one file is one transaction: register the file → `COPY` into `raw.<table>` with the
  lineage columns → record the counts → mark the file `loaded`. Any failure rolls back
  everything for that file.
* If a file with the same checksum is already `loaded`, the run does nothing and records that
  as an `ingestion_event`.
* `--force-reload` first deletes that file's `raw` rows (matched on `_source_file_id`) in the
  same transaction, then copies the file again.
* `raw → src` is rebuilt one table at a time inside a transaction (`TRUNCATE` +
  `INSERT … SELECT`), so rerunning it is idempotent by construction.
* Exact duplicate source records (identical in every column) are counted and kept in `raw` for
  lineage, then collapsed into a single `src` row with a WARNING. Records that share a key but
  differ in content are quarantined as ERROR: picking a winner silently would be inventing data.

## Consequences
+ A rerun produces the same state. This is tested by running the flow twice and comparing table
  fingerprints.
− Each file is one large transaction. That is fine at ≤ 1 M rows per file.
