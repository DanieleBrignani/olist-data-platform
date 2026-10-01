# Testing

**265 pytest tests** (158 unit, 89 integration, 18 end-to-end; 14 of them run on the real
dataset) plus **51 dbt data tests** that run inside every pipeline execution. Counts come from
`pytest --collect-only` and `dbt ls --resource-type test`.

Latest runs, 2026-09-29 to 2026-10-01: **265 passed, 0 failed, 0 skipped.** The 249 tests without the
real dataset (what CI's test job runs, `make test-ci`) took 16.6 min in a Linux container on a
clean export. The 14 real-data tests took 8.6 min.

**Line coverage: 93%** of `olist_platform` + `orchestration` over those 249 tests. The lowest
modules are the thin CLI wrapper (57%) and `orchestration/serve.py` (0%: it only starts the
long-running deployment server, and is exercised by `make up` rather than by tests).

## How to run

```bash
docker compose run --rm dev pytest                      # everything (needs Postgres; real-data tests need the dataset)
docker compose run --rm dev pytest tests/unit           # no database needed
docker compose run --rm dev pytest -m "not source_data" # everything except real-data tests (CI without the dataset)
docker compose run --rm dev pytest -m source_data       # only the real-data tests (slow: 3 full pipeline runs)
docker compose run --rm dev pytest --cov=olist_platform --cov=orchestration --cov-report=term
```

Integration and e2e tests run against a **separate database, `olist_dw_test`**, created by the
Postgres init script with the same roles and grants as `olist_dw`. They truncate tables freely
and can never touch the warehouse holding real data.

Markers: `integration` (needs Postgres), `e2e` (runs the real Prefect flow), `source_data`
(needs the downloaded Olist files; skipped with an explicit reason otherwise).

## Required test categories → where they are proven

### Unit tests
| Required | Tests |
|----------|-------|
| schema validation | `tests/unit/test_schema_check.py`: missing/renamed/duplicate/extra/reordered columns, BOM expected/unexpected, CRLF, invalid UTF-8, empty header. `test_contracts.py`: 9 kinds of invalid contract, FK type mismatch, identifier injection |
| checksum handling | `test_manifest_and_records.py`: size / sha256 / missing-file mismatch, manifest written once, lock covers every contract file |
| deduplication | exact-duplicate collapse and primary-key conflicts are SQL, so they are tested in integration (`test_staging.py`); record-structure rules are tested in `test_manifest_and_records.py` |
| transformations | `test_staging_compile.py`: rule → SQL compilation, FK ordering, cycle detection, NULL-passes semantics, prefilter shape |
| CLI exit codes | `test_cli.py`: exit 1 on breaking schema change / missing file; commands available |
| business rules | `test_quality_gate.py`: WARNING/ERROR/CRITICAL blocking semantics, tolerances. `test_dq_classification.py`: every dbt test classified, dbt severity mirrors the gate, mandatory rules present. `test_orchestration_policy.py`: retry only on transient errors |

### Integration tests
| Required | Tests |
|----------|-------|
| file → Postgres | `test_ingestion.py`: rerun no-op, forced reload without duplicates, values stored verbatim, malformed record quarantined, reject-ratio rollback with evidence kept, breaking header stops before writes, tampered file, unbalanced quotes |
| Postgres → dbt | `test_dbt_models.py`: the real dbt project on a hand-computable scenario; GMV, AOV, late flag, repeat customer and payment methods pinned to exact values |
| quality failure → quarantine | `test_staging.py`: 7 ERROR rules quarantined, WARNING flagged, cascade to children, key conflicts, reconciliation invariant. `test_quality_gate_publish.py`: failing warehouse rows stored in `meta.rejected_records` |

Other integration coverage: least-privilege roles (`test_database_roles.py`), the metrics
exporter against seeded metadata (`test_exporter.py`), retry behaviour on a real Prefect engine
(`test_orchestration_retries.py`), and fingerprint sensitivity (`test_fingerprint.py`).

### End-to-end: raw files → final marts
| Test | Data | Proves |
|------|------|--------|
| `tests/e2e/test_flow_e2e.py::test_flow_runs_all_steps_and_publishes` | synthetic | all 11 flow tasks run, marts published and readable by the reporting role |
| `tests/e2e/test_real_pipeline.py::test_raw_files_to_marts_accounts_for_every_record` | **real Olist v2** | loaded + rejected = every locked record; published marts reconcile with the source files |

### Idempotency: run the pipeline twice and prove the result is correct
| Test | Data | Proves |
|------|------|--------|
| `test_flow_e2e.py::test_idempotency_running_the_flow_twice_gives_identical_published_data` | synthetic | a forced rebuild of identical inputs republishes identical fingerprints for all 16 tables; ingestion skipped on the 2nd run |
| `test_real_pipeline.py::test_every_run_publishes_byte_identical_tables` | **real Olist v2** | 3 full runs (initial, plain rerun, forced reload of every file): every published table has the same row count **and** content md5 |
| `test_real_pipeline.py::test_reruns_skip_or_replace_but_never_duplicate` | **real Olist v2** | the plain rerun is skipped by change detection, the forced reload rebuilds; raw holds exactly one copy of each file; 9 source files registered, not 27 |

Incremental behaviour (new, modified and deleted rows; repeated and older snapshots; logic
changes; rollback) is covered on the real flow by `tests/e2e/test_incremental.py`, mapped
requirement by requirement in [incremental.md](incremental.md).

"Identical" means the fingerprint (`src/olist_platform/database/fingerprint.py`): row count +
md5 over every row in canonical order. `test_fingerprint.py` proves it detects a one-cent
change, NULL vs '', a lost row and a duplicate row, while ignoring insertion order.

### Failure injection: 28 tests

22 test functions, 28 collected tests (`test_error_rules_quarantine` runs once per rule). Each
injects a failure into a running system and asserts the platform's response.
Scenario-level view: [failure-recovery.md](failure-recovery.md).

| Test | Injected condition | Response asserted |
|------|--------------------|-------------------|
| `test_ingestion.py::test_file_changed_after_lock_fails_manifest_check` | file edited after locking | manifest check fails |
| `test_ingestion.py::test_breaking_header_stops_before_any_write` | renamed column | stops before any DB write |
| `test_ingestion.py::test_malformed_record_is_quarantined_not_dropped` | wrong field count | record quarantined with its original fields |
| `test_ingestion.py::test_reject_ratio_over_limit_fails_file_but_keeps_evidence` | too many bad records | file rolled back, rejects kept |
| `test_ingestion.py::test_unbalanced_quotes_reject_the_whole_file` | corrupt quoting | whole file rejected |
| `test_staging.py::test_uncastable_value_is_quarantined_with_reason` | value of the wrong type | quarantined with reason |
| `test_staging.py::test_error_rules_quarantine` | 7 ERROR-rule violations | each quarantined |
| `test_staging.py::test_quarantined_parent_cascades_to_children` | bad parent record | children quarantined too |
| `test_staging.py::test_key_conflict_quarantines_every_version_and_never_picks_a_winner` | same key, different content | every version quarantined |
| `test_staging.py::test_reject_ratio_over_limit_leaves_src_untouched_but_keeps_evidence` | reject ratio above the limit | `src` unchanged, evidence committed |
| `test_quality_gate_publish.py::test_injected_invalid_source_condition_blocks_publication` | cross-file violation valid per every file contract | CRITICAL test → gate FAIL → nothing published |
| `test_quality_gate_publish.py::test_publish_refuses_without_a_pass_decision` | publish called without a PASS | refused |
| `test_exporter.py::test_database_outage_reports_down_instead_of_crashing` | metadata DB unreachable | `db_up 0`, no crash |
| `test_orchestration_retries.py::test_transient_error_is_retried_until_success` | transient error | retried |
| `test_orchestration_retries.py::test_transient_retries_are_bounded` | persistent transient error | retries stop at the bound |
| `test_orchestration_retries.py::test_deterministic_error_is_not_retried_even_when_retries_are_configured` | deterministic error | one attempt |
| `test_flow_e2e.py::test_gate_failure_stops_flow_at_quality_gate_and_keeps_published_data` | gate failure in the flow | fails at `quality_gate`, previous version kept |
| `test_flow_e2e.py::test_tampered_source_fails_at_verify_manifest_before_any_write` | tampered file in the flow | fails at `verify_manifest`, nothing written |
| `test_failure_recovery.py::test_postgres_unavailable_is_a_transient_failure` | database unreachable | classified transient |
| `test_failure_recovery.py::test_dbt_model_failure_publishes_nothing_and_is_not_retried` | broken dbt model | one attempt, nothing published |
| `test_failure_recovery.py::test_killed_run_is_closed_and_leftovers_are_never_published` | run killed mid-build | closed as abandoned; leftovers never published |
| `test_failure_recovery.py::test_second_run_after_a_failed_run_publishes_the_fixed_data` | gate failure, then fixed data | next run rebuilds and publishes |

## Synthetic data policy

Synthetic records exist only in `tests/synthetic.py`, which writes small datasets in each
contract's real file format (BOM, CRLF, quoting). They are used for unit, integration and
failure tests only. **No benchmark or reported result uses synthetic data.**
