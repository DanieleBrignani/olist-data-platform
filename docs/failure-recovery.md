# Failure recovery

**Guarantee:** whatever fails, consumers keep reading the last complete, quality-gated version,
because the published `warehouse`/`marts` schemas only change in the single-transaction swap
at the end of a successful run (ADR-0005). Deterministic failures are never retried. The next
run recovers without manual cleanup.

| # | Scenario | What happens | Recovery | Evidence |
|---|----------|--------------|----------|----------|
| 1 | PostgreSQL unavailable | connection errors are classified **transient** (`TransientError`, SQLAlchemy/psycopg `OperationalError`); the task retries with exponential backoff, then fails the run | rerun when the DB is back; nothing was published | `test_postgres_unavailable_is_a_transient_failure`, `tests/integration/test_orchestration_retries.py`, `tests/unit/test_orchestration_policy.py` |
| 2 | Invalid input file (changed after locking, corrupt quoting) | `verify_manifest` / `ingest_raw` raise `ManifestMismatchError` / `DataContractError`; **not retried**; the file's transaction rolls back | fix or re-fetch the file | `test_tampered_source_fails_at_verify_manifest_before_any_write`, `test_unbalanced_quotes_reject_the_whole_file` |
| 3 | Schema drift | breaking change (missing/renamed column, BOM change) fails `validate_source` **before any DB write**; extra/reordered columns are logged only | update the contract deliberately | `tests/unit/test_schema_check.py`, `test_breaking_header_stops_before_any_write` |
| 4 | Duplicate rows | exact duplicates collapsed (counted); same key with different content → every version quarantined; reloads replace, never append | – (handled) | `tests/integration/test_staging.py`, `test_no_duplicates_after_repeated_versions` |
| 5 | Corrupted values | record-level rules quarantine (ERROR) or flag (WARNING) with rule, severity, run, reason; above the reject limit the table is rejected and `src` stays unchanged | fix the source; the evidence is in `meta.rejected_records` | `tests/integration/test_staging.py`, `test_reject_ratio_over_limit_leaves_src_untouched_but_keeps_evidence` |
| 6 | Quality-gate failure | CRITICAL (or ERROR above tolerance) → FAIL; **not retried**; build schemas are not swapped in | fix the data; the next run rebuilds because that input was never published | `test_injected_invalid_source_condition_blocks_publication`, `test_second_run_after_a_failed_run_publishes_the_fixed_data` |
| 7 | dbt model failure | `DbtError` is deterministic: **one attempt**, run fails at `dbt_build`, nothing published | fix the model; the logic change also changes the input fingerprint, forcing a rebuild | `test_dbt_model_failure_publishes_nothing_and_is_not_retried` |
| 8 | Interrupted pipeline (process killed) | a killed run never reaches its failure handler, so it stays `running`; its half-built `*_build` schemas remain | the next run closes runs still `running` after 3 h (flow timeout 2 h) as `failed/abandoned`, and every build starts from **empty** build schemas, so leftovers can never be published | `test_killed_run_is_closed_and_leftovers_are_never_published`, `test_recent_running_rows_are_not_closed` |
| 9 | Second execution after a failed run | failed runs publish nothing, so their inputs still differ from the published fingerprint and the next run rebuilds | automatic | `test_second_run_after_a_failed_run_publishes_the_fixed_data` |
| 10 | Second execution after a successful run | identical inputs → `detect_changes` skips staging, dbt, gate and publication; published tables unchanged | automatic | `test_first_run_loads_everything_and_identical_rerun_changes_nothing`, `tests/e2e/test_real_pipeline.py` |
| 11 | Concurrent operations (a CLI step during a deployment run, a second worker) | every operation that writes pipeline state (the flow, the CLI `ingest`, `stage`, `transform`, `warehouse`, `run`, publication, rollback, `db drop-derived`) holds one PostgreSQL advisory lock for its whole duration; a contender waits up to 60 s (`OLIST_PIPELINE_LOCK_WAIT_SECONDS`), then fails as `pipeline_busy`, a transient error naming the holder's pid; it writes nothing, and readers are never blocked | rerun when the holder finishes; a killed holder's lock ends with its session | `test_a_concurrent_operation_blocks_the_whole_flow_and_nothing_is_published`, `tests/integration/test_pipeline_lock.py` (separate processes, SIGKILL, nesting, timeout) |

Also covered: a bad publication discovered later is undone with `olist rollback-publish`
(metadata-only swap back to `*_prev`, `test_rollback_restores_previous_publication_and_revokes_prev`),
and the next run then correctly rebuilds the current inputs
(`test_after_rollback_the_next_run_rebuilds_the_current_inputs`).

## Two defects this review found and fixed

* **Stale tables could be published.** dbt only (re)creates *current* models, and the build
  schemas survived a failed gate or a `--no-publish` run, so a table left over from a removed
  model could be swapped into the published warehouse. `dbt_build` now drops the build schemas
  before every build (`src/olist_platform/transform/warehouse.py`).
* **Killed runs stayed `running` forever,** leaving a phantom run in `pipeline_runs_in_progress`.
  They are now closed as `abandoned` when the next run starts
  (`src/olist_platform/ingestion/runs.py`).

## What is not protected

* A crash **during** the swap transaction is safe: PostgreSQL rolls it back. A crash of the
  PostgreSQL volume itself is not: see [backup-and-recovery.md](backup-and-recovery.md).
* Two operations from **different databases** (e.g. the real and the test warehouse) do not
  exclude each other: the lock is per database, by design.
* A process that keeps its lock connection open but stops making progress (hung, not dead)
  blocks others until the flow's 2 h timeout or an operator ends its session
  (`pg_terminate_backend(<pid>)`; the pid is in the `pipeline_busy` error).
