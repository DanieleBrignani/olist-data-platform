# What "incremental" means in this project

The Olist dataset is a **static historical extract**: nine files, each a *full snapshot* of one
table, with no `updated_at` column and no change-data-capture feed. That fact decides what an
honest incremental strategy can be.

## The policy

| Question | Answer in this project |
|----------|------------------------|
| Unit of change | a **source-file snapshot**, identified by its sha256 |
| New data arrives as | a new version of a table's file (a new snapshot) |
| Which snapshot is used | the **latest loaded** file per table (`meta.current_source_files`) |
| Modified rows | the latest snapshot wins |
| Deleted rows | rows absent from the latest snapshot disappear from `src` and the warehouse |
| History | `raw` keeps every loaded snapshot, with lineage, forever |
| Identical file loaded again | recorded no-op (`skipped_already_loaded`); `--force-reload` replaces its rows, never appends |
| When the pipeline rebuilds | only when the **input fingerprint** differs from the published version's |
| Input fingerprint | sha256 over the current snapshot checksums **plus** the transformation logic (dbt project, contracts, staging engine) |
| How to force a rebuild | `olist run --full-refresh` (also implied by `--force-reload`) |
| Backfill / restore an older snapshot | `olist ingest --force-reload` with the older file makes it the latest again; the next run rebuilds from it |
| Late-arriving data | arrives inside a newer snapshot, so it is processed as a changed input |

## Why snapshot-replace and not row-level MERGE

A row-level upsert on business keys would need an `updated_at` (to order versions) and a
delete signal (tombstones or CDC); the source has neither. With full snapshots,
"latest snapshot = complete current state" is exact: it handles inserts, updates **and deletes**
without guessing. MERGE would add code paths that cannot be driven correctly by this data.

## What is skipped, and why that is safe

`detect_changes` runs after ingestion (`orchestration/olist_flow.py`,
`src/olist_platform/database/change_detection.py`):

```
ingest_raw ─► detect_changes ─┬─ inputs changed / forced ─► load_staging ─► dbt_build ─► dbt_test ─► quality_gate ─► publish_marts
                              └─ inputs unchanged ────────► (skip; event 'skipped_unchanged')
                                                                                   ─► publish_metrics (always)
```

Skipping is safe because a publication is a pure function of its inputs: the real-data
idempotency tests show that rebuilding from identical inputs produces byte-identical tables
(`tests/e2e/test_real_pipeline.py`). Including the **transformation logic** in the fingerprint
matters: without it, a changed dbt model on unchanged data would leave a stale warehouse.

Each `meta.publications` row stores the fingerprint of the version that is **active after it**.
A rollback row therefore carries the fingerprint of the version it restored, so the next run
correctly rebuilds the current inputs.

## Why dbt models stay full-refresh inside a rebuild

A rebuild builds into `*_build` schemas and swaps them in atomically (ADR-0005). An incremental
dbt model needs persistent state in its own schema, and the swap would move that state away.
The source also has no `updated_at` to drive `is_incremental()` filters (ADR-0006). A full
rebuild of the warehouse takes about 25 s on this dataset (benchmark), so the saving would be small
and the risk real.

## Evidence

`tests/e2e/test_incremental.py`, on the real flow:

| Requirement | Test |
|-------------|------|
| first run loads all expected data | `test_first_run_loads_everything_and_identical_rerun_changes_nothing` |
| identical rerun changes nothing (no staging, no dbt, no new publication, identical tables) | same test |
| new data is processed | `test_new_modified_and_deleted_rows_follow_the_snapshot_policy` |
| modified and deleted data follow the policy | same test |
| no duplicates across repeated and older snapshots | `test_no_duplicates_after_repeated_versions` |
| full refresh == clean reconstruction | `test_full_refresh_equals_clean_reconstruction` |
| changed logic forces a rebuild | `test_changed_transformation_logic_triggers_rebuild` |
| correct decision after a rollback | `test_after_rollback_the_next_run_rebuilds_the_current_inputs` |

The decision rule and the fingerprint (including Windows/Linux line-ending independence) are
unit-tested in `tests/unit/test_change_detection.py`.
