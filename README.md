# Production Data Platform — Olist Brazilian E-Commerce

> **Build status: Phase 12 of 14 (benchmark).** Sections marked *pending* are filled in by
> the phase that produces their evidence. No section contains numbers that have not been
> measured by code in this repository.

## 1. Business Question

**How can fragmented ecommerce operational data be transformed into a trusted,
analytics-ready data warehouse that can be safely refreshed and used for business reporting?**

## 2. Business Problem

Olist's operational data is spread across nine related CSV extracts. To answer a simple
question such as "what share of orders arrived late last quarter, by seller state?", an
analyst currently has to:

* join orders, items, sellers, customers and geolocation by hand;
* understand OLTP schemas and status codes;
* work around duplicates (for example, repeated geolocation rows and repeated review ids);
* spot inconsistent records (such as delivery before purchase, or orders with no items);
* rebuild the same metric definitions every time, with no tests behind them.

## 3. Objective

Build an end-to-end, reproducible platform that ingests the real Olist dataset, keeps the raw
data immutable, enforces source contracts, and builds a tested dimensional warehouse and
business marts with dbt. It publishes those marts only when the quality gates pass,
orchestrates everything with retries and timeouts, and exposes operational metrics.

## 4. Measured Results

<!-- BENCHMARK:START -->
Measured by `scripts/benchmark.py` on 2026-09-27 (commit `dee7d90`), real Olist v2 data, median of 3 repetitions from an empty database. Environment and method: [docs/benchmark.md](docs/benchmark.md).

| Measure | Result |
|---------|--------|
| Source records ingested (9 files) | 1,550,922 |
| Records quarantined / flagged by contracts | 0 / 542 |
| Data-quality tests evaluated / warnings / gate | 36 / 6 / PASS |
| Initial load, end to end (empty DB → published marts) | 89.1 s (min 69.2 / max 107.7) |
| Rerun with unchanged files (checksum skip + rebuild + gate + publish) | 75.7 s (min 50.3 / max 82.8) |
| Ingestion throughput | 73,406 rows/s |
| Staging throughput (raw → typed, all rules) | 41,659 rows/s |
| dbt build (33 models) + dbt test (36 tests) | 25.1 s (min 17.4 / max 26.9) + 3.2 s (min 3.1 / max 4.5) |
| Database size after load | 734.0 MB |
| Reporting query latency, median (p95) | Q1 0.2 ms (0.4); Q2 15.5 ms (15.9); Q3 0.3 ms (0.4); Q4 0.2 ms (0.2); Q5 0.6 ms (0.8); Q6 0.2 ms (0.2) |
| Published tables identical across all 3 repetitions (content md5) | yes |
<!-- BENCHMARK:END -->

## 5. Architecture Diagram

See [docs/architecture.md](docs/architecture.md). Decisions are in [docs/adr/](docs/adr/README.md).

## 6. Data Flow

Implemented so far (Phases 3–5); later phases extend this section.

| Step | What happens | On failure |
|------|--------------|------------|
| `download_or_locate_source` | `olist source fetch`: uses local files if all 9 are present, else downloads Kaggle v2 and makes the files read-only | network → retryable; partial raw dir → refused |
| `verify_manifest` | recomputes size + sha256 of every file against the committed `data_contracts/manifest.lock.json` | `ManifestMismatchError`, never retried, nothing written |
| `validate_source` | checks every file's encoding and header against its contract **before any DB write** | breaking change → `DataContractError` |
| `ingest_raw` | streams each file (csv → `COPY`) into `raw.<table>` in one transaction per file, with lineage (`_source_file_id`, `_row_number`, `_pipeline_run_id`); structurally broken records go to `meta.rejected_records`; loaded + rejected must equal the locked row count | reject ratio over the contract limit → file rolled back, evidence kept |
| `load_staging` | rebuilds all `src.*` typed tables in **one transaction** from the latest loaded file per table. It checks types, not-nulls, patterns, contract rules, key conflicts and foreign keys. ERROR records are quarantined, WARNING records are flagged and kept, exact duplicates are collapsed with a count. See [docs/database_design.md](docs/database_design.md) | a reject ratio over the contract limit → `src` untouched, evidence committed, `DataContractError` |

Every run is recorded in `meta.pipeline_runs`, every file in `meta.source_files`
(unique on `(source_table, sha256)`), and every step in `meta.ingestion_events`.
Re-running with unchanged files is a recorded no-op; `--force-reload` replaces a file's rows.

## 7. Dataset

| Property | Value | How it was verified |
|----------|-------|---------------------|
| Source | Kaggle `olistbr/brazilian-ecommerce` ("Brazilian E-Commerce Public Dataset by Olist") | Kaggle public API `datasets/view`, 2026-09-26 |
| Version | 2 (`currentVersionNumber`), last updated 2021-10-01T19:08:27Z | same |
| Licence | CC BY-NC-SA 4.0 (non-commercial, attribution, share-alike) | same |
| Files | 9 CSV, 126,186,995 bytes total; every downloaded file matches Kaggle's reported size | Kaggle public API `datasets/list`, `ls -l` after `olist source fetch` |
| Download date | 2026-09-26 (archive sha256 `967e41e0…deca784`, logged by `olist source fetch`) | structured log `source_downloaded` |
| File checksums + row counts | [`data_contracts/manifest.lock.json`](data_contracts/manifest.lock.json) (committed); `data/raw/olist/v2/manifest.json` (local, read-only) | `olist source manifest --write-lock` |

Record counts measured by [`scripts/profile_sources.py`](scripts/profile_sources.py) (full report:
[docs/source_profile.md](docs/source_profile.md)):

| File | Records | Notable measured facts |
|------|--------:|------------------------|
| olist_orders_dataset.csv | 99,441 | 8 statuses; 166 carrier dates before purchase; 0 deliveries before purchase |
| olist_customers_dataset.csv | 99,441 | `customer_unique_id` repeats 3,345 times (repeat buyers) |
| olist_order_items_dataset.csv | 112,650 | covers 98,666 orders (some orders have no items) |
| olist_order_payments_dataset.csv | 103,886 | 3 `not_defined` payment types, 9 zero-value payments |
| olist_order_reviews_dataset.csv | 99,224 | `review_id` not unique (814 extra); multi-line comments; CRLF |
| olist_products_dataset.csv | 32,951 | 610 products without category; misspelled `*_lenght` columns |
| olist_sellers_dataset.csv | 3,095 | 1 numeric city name |
| olist_geolocation_dataset.csv | 1,000,163 | 261,831 exact duplicate records; 31 points outside Brazil |
| product_category_name_translation.csv | 71 | UTF-8 **BOM** + CRLF; 2 product categories have no translation |

**Filtering rules:** no source record is deleted. Records that violate an ERROR rule are
quarantined in `meta.rejected_records` with the rule, severity, run id and reason. WARNING
violations are kept and counted. Exact duplicate geolocation records are collapsed in `src`
and still counted in `raw`. Contracts: [docs/data_contracts.md](docs/data_contracts.md).

## 8. Data Model

Star schema built with dbt: 5 dimensions (date, customer = *person*, product, seller,
geography) and 4 facts at different grains (order, order line, payment, review). Dimensions
and facts have **enforced** contracts: 9 primary keys and 20 foreign keys exist as real
PostgreSQL constraints. Seven business marts cover sales, payment methods, delivery
performance, customer behaviour, seller performance, category performance and review
distribution.

Full documentation, including business process, grain, keys and additive vs non-additive
measures for each fact, and why each dimension exists: [docs/data_model.md](docs/data_model.md).
Key strategy: [ADR-0011](docs/adr/0011-natural-keys-no-scd2.md).

## 9. Data Quality Strategy

Three checkpoints share one severity vocabulary (WARNING / ERROR / CRITICAL):
1. **File checks:** checksum lock and schema contract. A breaking change stops the run
   before any write.
2. **Record checks (raw → src):** contract rules. ERROR records are quarantined, WARNING
   records are flagged, and both are stored with rule, severity, run id, timestamp and reason.
3. **Model checks (dbt, 40+ tests):** including CRITICAL reconciliation of `src` against the
   warehouse to the cent, marts against facts, and cross-file timeline invariants.

A **quality gate** turns the dbt results into PASS/FAIL. CRITICAL failures, and ERROR
failures beyond their tolerance, block `publish_marts`, so the published warehouse stays
untouched. Every threshold was set *after* measuring the rule on the real data.

Details, the rule-coverage matrix and measured baselines: [docs/data_quality.md](docs/data_quality.md).

## 10. Orchestration

One Prefect 3 flow, `olist_refresh`, runs the whole path as **one tracked pipeline run**:

`download_or_locate_source → verify_manifest → validate_source → ingest_raw → load_staging → dbt_build → dbt_test → quality_gate → publish_marts → publish_metrics`

* **Timeouts:** every task has one, and the flow has 2 hours.
* **Retries are decided by error type:** transient errors (network, DB connection, lock
  timeout) get bounded retries with exponential backoff and jitter. Deterministic errors
  (checksum, contract, quality gate, dbt model) fail immediately. This is proven on a real
  Prefect engine.
* **Failure callbacks:** tasks and the flow emit structured `task_failed` / `flow_failed`
  events, and the failing task is recorded in `meta.pipeline_runs`.
* **No result caching:** a step with side effects is never skipped.
* **Runtime:** a Prefect server (UI on http://localhost:4200) plus a worker serving the
  `olist-refresh` deployment, one run at a time.

Details and the per-task policy table: [docs/orchestration.md](docs/orchestration.md).

## 11. Observability

```
pipeline ──► meta.* tables (system of record) ──► metrics-exporter ──► Prometheus ──► Grafana
    └──► structured JSON logs (stdout + logs/pipeline.jsonl)          (recording + alert rules)
```

* **Logs:** every line is JSON, including third-party libraries, with `timestamp`,
  `environment`, `pipeline_run_id`, `task`, `source`, `rows_read`, `rows_written`,
  `rows_rejected`, `duration_ms`, `status` and `error_type`.
* **Metrics:** derived at scrape time from the append-only metadata tables, not pushed.
  Counters therefore stay monotonic across batch processes and agree with SQL:
  `pipeline_runs_total`, `pipeline_failures_total`, `pipeline_duration_seconds`,
  `rows_processed_total`, `rows_rejected_total`, `data_quality_failures_total`, and
  `dataset_freshness_seconds` (a Prometheus recording rule).
* **Dashboard:** Grafana (http://localhost:3000) shows success rate, runtime, rows processed,
  quarantined/flagged records, quality failures and freshness, plus the gate decision and the
  2018 source-data horizon. It is provisioned from code, and a test guarantees every panel only
  uses metrics the exporter really exposes.
* **Alerts:** pipeline failed, quality gate failed, warehouse stale, metrics unavailable.

Details: [docs/observability.md](docs/observability.md).

## 12. Benchmark

`scripts/benchmark.py` measures the real pipeline on the real dataset. It runs in an isolated
compose project with a fresh database, 3 repetitions each starting from an **empty** warehouse,
and reports median/min/max, the environment and the git commit. Every number in section 4 and
in [docs/benchmark.md](docs/benchmark.md) (per-step timings, database size by schema, query
latency) comes from that run; raw output is in `benchmark/results.json`, and a test keeps the
README block in sync with it.

What the measurements show:
* **The marts pay off.** The monthly GMV/AOV dashboard query is about two orders of magnitude
  faster from `mart_sales` than the same answer computed from `fct_orders`. This confirms the
  Phase 5 plan finding that full-history aggregation cannot be fixed by an index.
* **Checksum idempotency works, but a rerun still rebuilds everything downstream.**
  Ingestion drops to a no-op, yet staging and dbt run in full, so a rerun costs most of an
  initial load. This is the quantified price of the full-refresh design (ADR-0006), and
  staging is the first place to optimise.
* **Lineage has a storage cost.** Keeping a verbatim text copy in `raw` (for record-level
  traceability) is a large share of the database size.
* **Determinism.** All published tables are byte-identical across the three repetitions.
* **The host is noisy** (a memory-constrained laptop with other containers running), which is
  why a spread is reported instead of a single run.

Reproduce:

```bash
docker compose -p olistbench up -d --wait postgres
docker compose -p olistbench up --exit-code-from migrate migrate
docker compose -p olistbench run --rm -e BENCHMARK_GIT_COMMIT=$(git rev-parse --short HEAD) dev python scripts/benchmark.py --reset --update-readme
docker compose -p olistbench down -v
```

## 13. Engineering Decisions
See [docs/adr/](docs/adr/README.md).

## 14. Trade-offs
*Pending (Phase 13).*

## 15. Failure Cases

Each case is injected by an automated test, and the platform's response is asserted. Details:
[docs/testing.md](docs/testing.md).

| Failure | Where it is stopped | What the system guarantees |
|---------|---------------------|----------------------------|
| Source file changed after locking (same size) | `verify_manifest` | run fails (never retried), nothing written |
| Column missing / renamed, BOM appears, invalid UTF-8 | `validate_source` | run fails before any DB write; extra or reordered columns are only logged |
| Malformed record (wrong field count, NUL byte) | `ingest_raw` | record quarantined with its original fields and row reference; file loads |
| Unbalanced quoting / too many bad records | `ingest_raw` | the file's load is rolled back; rejects and a failure event are still persisted |
| Value breaks a contract rule (type, range, accepted values, timeline) | `load_staging` | ERROR record quarantined (children cascade), WARNING record flagged and kept |
| Same key, different content | `load_staging` | every version quarantined; no winner is picked |
| Cross-file violation invisible to file contracts | dbt CRITICAL test → `quality_gate` | gate FAILS, publication blocked, previous marts stay published, failing rows stored |
| Transformation bug (lost / duplicated / rounded rows) | CRITICAL reconciliation tests | gate FAILS |
| Network / DB connection drop | any task | bounded retries with exponential backoff; deterministic errors are never retried |
| Long report holds the published schema | `publish_marts` | `lock_timeout` → transient → retried |
| Bad publication discovered later | operator | `olist rollback-publish` swaps back to the previous version |
| Metadata DB unreachable | metrics exporter | scrape succeeds with `olist_exporter_db_up 0`, and an alert fires |

## 16. What Failed and What I Changed
*Kept as a running log while the phases are built. Filled in during Phase 13.*

## 17. Known Limitations
*Pending (Phase 14).*

## 18. How to Run

Prerequisites: Docker with Compose v2. No local Python is needed.

```bash
python scripts/make_env.py  # creates .env from .env.example with random secrets
docker compose up -d --wait postgres
docker compose up --build migrate
docker compose run --rm dev olist doctor
docker compose run --rm dev olist source fetch            # ~126 MB from Kaggle, read-only
docker compose run --rm dev olist contracts check-source  # exit 1 on breaking schema change
docker compose run --rm dev olist ingest                  # rerun = no-op; --force-reload to replace
docker compose run --rm dev olist stage                   # raw -> typed src, one transaction
docker compose run --rm dev olist transform               # dbt build -> warehouse_build, marts_build
docker compose run --rm dev olist warehouse               # dbt run -> dbt test -> quality gate -> publish
docker compose run --rm dev olist rollback-publish        # swap back to the previous publication
docker compose up -d prefect-server pipeline-worker      # orchestration (UI: http://localhost:4200)
docker compose exec pipeline-worker prefect deployment run olist_refresh/olist-refresh
docker compose up -d metrics-exporter prometheus grafana  # metrics :9108, Prometheus :9090, Grafana :3000
docker compose run --rm dev python scripts/explain_queries.py  # regenerates docs/query_plans.md
```

## 19. How to Test

```bash
docker compose run --rm dev pytest                       # all 233 tests (93% line coverage)
docker compose run --rm dev pytest -m "not source_data"  # without the real dataset (219 tests)
docker compose run --rm dev pytest -m source_data        # real-data e2e + idempotency (3 full runs)
docker compose run --rm --no-deps dev sh -c "ruff check . && ruff format --check ."
```

The suite covers unit, integration (file → Postgres, Postgres → dbt, quality failure →
quarantine), end-to-end (raw files → marts, synthetic **and real**), idempotency (3 full
real-data runs producing byte-identical published tables) and failure injection. Mapping of
each required category to its tests: [docs/testing.md](docs/testing.md).

### Continuous integration

GitHub Actions (`.github/workflows/ci.yml`) runs four jobs:
* **lint:** ruff lint and format, `promtool`, `actionlint`.
* **test:** reversible migrations, contracts, `dbt compile`, and 219 tests with the dbt tests
  inside.
* **real-data:** the full pipeline on the real dataset. A CRITICAL data-quality failure exits 1
  and fails CI.
* **images:** Docker builds.

Details: [docs/ci.md](docs/ci.md).

## 20. Final Answer to the Business Question
*Written in Phase 13, based on the measured results.*
