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
| Input fingerprint | sha256 over the current snapshot checksums **plus** the transformation logic (dbt project, contracts, staging engine) **and** the validation policy (gate, dbt result parsing, contract model) |
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

## What counts as an input, and why the validation policy does

The fingerprint covers everything that, for given source files, decides the published tables
**or the decision to publish them** (`TRANSFORM_INPUTS` in
`src/olist_platform/database/change_detection.py`):

| Input | Why |
|-------|-----|
| `dbt/` project, models, macros, tests | transformations, test definitions, severities and tolerances |
| `data_contracts/*.yml`, `database/staging.py` | record rules, quarantine / flag / reject |
| `validation/contracts.py` | how contract rules and severities are read |
| `transform/dbt_runner.py`, `transform/warehouse.py` | how dbt results become gate inputs; which tests run |
| `quality/gate.py` | blocking semantics, default severity, tolerances |

Not included, deliberately: `publish.py` (refuses without a PASS but cannot change the
decision), `dbt/profiles.yml` (connection, threads), the orchestration (control flow), and
`ingestion/loader.py` (raw is keyed by file checksum: an unchanged file is never re-ingested,
whatever the loader does; reprocessing needs `--force-reload`, which implies a full refresh).

**A policy-only change** (same data, same models, stricter or looser gate) therefore rebuilds
and produces a new gate decision. If the new policy **fails**, nothing is published and the
previous version stays active. That version was validated under the **old** policy only: it is
kept so that consumers are not left without data, not because it satisfies the new rules. The
active publication keeps the old fingerprint, so every later run rebuilds and re-evaluates
until the data or the policy changes; each failure is recorded and alerted as a gate failure.

## Why dbt models stay full-refresh inside a rebuild

A rebuild builds into `*_build` schemas and swaps them in atomically (ADR-0005). An incremental
dbt model needs persistent state in its own schema, and the swap would move that state away.
The source also has no `updated_at` to drive `is_incremental()` filters (ADR-0006). The dbt
build takes about 28-32 s on this dataset ([benchmark.md](benchmark.md)), so the saving would be
small and the risk real.

## Measured effect

Median of 3 repetitions on the real data ([benchmark.md](benchmark.md)): an unchanged rerun
takes **2.1 s**, against **94.6 s** for a forced rebuild of the same inputs. Most of the
remaining 2 s is re-hashing the 126 MB of source files to prove they are unchanged. In a
rebuild, `load_staging` is the largest step (46 s), so per-table incremental staging is the
next optimisation if inputs start changing often.

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
| a policy-only change forces a new gate decision (and unchanged reruns stay cheap) | `test_a_policy_only_change_forces_a_new_gate_decision_on_unchanged_data` |
| a stricter policy that fails keeps the old version, never certifies it, and re-evaluates on every run | `test_a_stricter_policy_that_rejects_published_data_keeps_it_but_never_certifies_it` |

The decision rule and the fingerprint (including Windows/Linux line-ending independence) are
unit-tested in `tests/unit/test_change_detection.py`.
