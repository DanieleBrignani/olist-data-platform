# Testing

**233 pytest tests** (137 unit, 89 integration, 7 end-to-end) plus **36 dbt data tests** that
run inside every pipeline execution (Phase 7). Counts come from `pytest --collect-only`.
**Line coverage: 93%** of `olist_platform` + `orchestration` (measured with pytest-cov on the
full suite; the lowest module is the thin CLI wrapper at 59%).

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
| `tests/e2e/test_flow_e2e.py::test_flow_runs_all_steps_and_publishes` | synthetic | all 10 flow tasks run, marts published and readable by the reporting role |
| `tests/e2e/test_real_pipeline.py::test_raw_files_to_marts_accounts_for_every_record` | **real Olist v2** | loaded + rejected = every locked record; published marts reconcile with the source files |

### Idempotency: run the pipeline twice and prove the result is correct
| Test | Data | Proves |
|------|------|--------|
| `test_flow_e2e.py::test_idempotency_running_the_flow_twice_gives_identical_published_data` | synthetic | identical fingerprints for all 16 published tables; ingestion skipped on the 2nd run |
| `test_real_pipeline.py::test_every_run_publishes_byte_identical_tables` | **real Olist v2** | 3 full runs (initial, plain rerun, forced reload of every file): every published table has the same row count **and** content md5 |
| `test_real_pipeline.py::test_reruns_skip_or_replace_but_never_duplicate` | **real Olist v2** | raw holds exactly one copy of each file after a forced reload; 9 source files registered, not 27 |

"Identical" means the fingerprint (`src/olist_platform/database/fingerprint.py`): row count +
md5 over every row in canonical order. `test_fingerprint.py` proves it detects a one-cent
change, NULL vs '', a lost row and a duplicate row, while ignoring insertion order.

### Failure tests: an injected invalid condition must be caught
| Test | Injected condition | Caught by |
|------|--------------------|-----------|
| `test_quality_gate_publish.py::test_injected_invalid_source_condition_blocks_publication` | an order line whose shipping deadline precedes its purchase: valid per every file contract | CRITICAL cross-file dbt rule → quality gate FAIL → published marts unchanged, evidence stored |
| `test_flow_e2e.py::test_gate_failure_stops_flow_at_quality_gate_and_keeps_published_data` | same, through the orchestrated flow | flow fails at `quality_gate`, not retried |
| `test_flow_e2e.py::test_tampered_source_fails_at_verify_manifest_before_any_write` | a source file edited after locking (same size) | `verify_manifest`, before any DB write |
| `test_ingestion.py`, `test_staging.py` | malformed records, reject ratio above the limit, breaking schema | quarantine / rollback with evidence kept |

## Synthetic data policy

Synthetic records exist only in `tests/synthetic.py`, which writes small datasets in each
contract's real file format (BOM, CRLF, quoting). They are used for unit, integration and
failure tests only. **No benchmark or reported result uses synthetic data.**
