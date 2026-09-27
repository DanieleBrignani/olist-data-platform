# Production Data Platform — Olist Brazilian E-Commerce

> **Build status: Phase 13 of 14 (documentation).** Sections marked *pending* are filled in by
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

```mermaid
flowchart LR
    K[("Kaggle<br/>Olist v2")] -->|download once| R["data/raw<br/>read-only CSVs<br/>+ checksum lock"]
    subgraph PG["PostgreSQL 16 (olist_dw)"]
        direction LR
        RAW["raw<br/>verbatim text<br/>+ lineage"] --> SRC["src<br/>typed, PK/FK/CHECK"]
        SRC --> STG["stg / int<br/>dbt views"] --> WB["warehouse_build<br/>dims + facts"] --> MB["marts_build<br/>business marts"]
        META[("meta<br/>runs, files, events<br/>quarantine, dq results")]
        WB -. "atomic swap<br/>if gate PASS" .-> W["warehouse"]
        MB -. "atomic swap<br/>if gate PASS" .-> M["marts"]
    end
    R -->|verify, validate, COPY| RAW
    M --> BI["analysts / BI<br/>(olist_reporting, read-only)"]
    W --> BI
    subgraph OPS["Operations"]
        PF["Prefect flow<br/>olist_refresh"]
        EX["metrics exporter<br/>(olist_monitor)"] --> PR["Prometheus<br/>+ alert rules"] --> GR["Grafana"]
    end
    PF -. orchestrates .-> PG
    META --> EX
```

Components, schemas and roles: [docs/architecture.md](docs/architecture.md). Every decision has
an ADR: [docs/adr/](docs/adr/README.md).

## 6. Data Flow

The ten steps of the `olist_refresh` flow, in order:

| Step | What happens | On failure |
|------|--------------|------------|
| `download_or_locate_source` | `olist source fetch`: uses local files if all 9 are present, else downloads Kaggle v2 and makes the files read-only | network → retryable; partial raw dir → refused |
| `verify_manifest` | recomputes size + sha256 of every file against the committed `data_contracts/manifest.lock.json` | `ManifestMismatchError`, never retried, nothing written |
| `validate_source` | checks every file's encoding and header against its contract **before any DB write** | breaking change → `DataContractError` |
| `ingest_raw` | streams each file (csv → `COPY`) into `raw.<table>` in one transaction per file, with lineage (`_source_file_id`, `_row_number`, `_pipeline_run_id`); structurally broken records go to `meta.rejected_records`; loaded + rejected must equal the locked row count | reject ratio over the contract limit → file rolled back, evidence kept |
| `load_staging` | rebuilds all `src.*` typed tables in **one transaction** from the latest loaded file per table. It checks types, not-nulls, patterns, contract rules, key conflicts and foreign keys. ERROR records are quarantined, WARNING records are flagged and kept, exact duplicates are collapsed with a count. See [docs/database_design.md](docs/database_design.md) | a reject ratio over the contract limit → `src` untouched, evidence committed, `DataContractError` |
| `dbt_build` | builds 17 views (`stg`, `int`) and 16 tables into `warehouse_build` / `marts_build`, with enforced contracts (real PK/FK) | model error → `DbtError`, not retried |
| `dbt_test` | 36 classified tests; failing rows stored in `dq_failures` | test failures are data, judged by the gate |
| `quality_gate` | CRITICAL, or ERROR above its tolerance → FAIL; results, sample rows and decision recorded | FAIL → run stops, nothing published |
| `publish_marts` | one transaction: `*_build` → `warehouse`/`marts`, previous version kept as `*_prev`, reporting grants moved | lock timeout → retried |
| `publish_metrics` | run summary from the metadata tables, emitted as a structured event | – |

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

Each decision is an ADR with context, alternatives rejected and consequences: [docs/adr/](docs/adr/README.md).

| ADR | Decision | Why (in one line) |
|-----|----------|-------------------|
| 0001 | One PostgreSQL, one schema per layer | FKs, dbt refs and the atomic publish swap all need one transaction; runs anywhere with Docker |
| 0002 | Immutable raw files pinned by a committed checksum lock | the data cannot be committed (licence, size), so the lock is what makes runs reproducible |
| 0003 | Prefect 3; retries decided by error type | typed retry conditions, the same flow runs in tests, lighter than Airflow on an 8 GB host |
| 0004 | WARNING / ERROR / CRITICAL at three checkpoints; GE/Soda evaluated and not adopted | one vocabulary, one gate; a second DQ framework would duplicate checks without a new guarantee |
| 0005 | Build into `*_build`, publish by atomic schema swap | a failed gate leaves consumers on the previous good version; rollback is a rename |
| 0006 | Full refresh, no incremental dbt models | static snapshot with no `updated_at`; determinism over speed (cost measured in section 12) |
| 0007 | Metrics derived from append-only metadata tables, not Pushgateway | counters stay monotonic across batch processes and agree with SQL |
| 0008 | Idempotent ingestion keyed by file sha256 | reruns are recorded no-ops; forced reloads replace, never duplicate |
| 0009 | Five least-privilege roles | a leaked reporting credential cannot read unpublished data or write anything |
| 0010 | No BI tool in the core stack | the consumption contract is the `marts` schema + reporting role |
| 0011 | Source ids as dimension keys, no SCD2 | a single snapshot has no history; SCD2 would be fabricated |

## 14. Trade-offs

| Choice | What it buys | What it costs (measured where possible) |
|--------|--------------|------------------------------------------|
| Full refresh of `src` and dbt on every run | deterministic, byte-identical results; no merge logic to get wrong | a rerun with unchanged files costs most of an initial load (section 4); staging is the first optimisation target |
| Verbatim `raw` copy with lineage | every quarantined record is traceable to its file, row number and original text | `raw` is roughly a third of the database size (section 12) |
| Streaming csv → `COPY` in Python instead of server-side `COPY FROM file` | per-record quarantine: one bad line does not abort a 1 M-row file | slower than a native bulk load; still about 73k rows/s here |
| Custom contract engine instead of Great Expectations/Soda | one severity model and one quarantine table from file to warehouse | code to own and test (staging engine 99% covered) |
| Enforced dbt contracts with real PK/FK | the database rejects a broken transformation; column drift fails the build | every column type declared twice (SQL + YAML) |
| Build-then-swap publication | consumers never see a partial or failed build; instant rollback | needs an exclusive lock for the rename; long reports can delay publication (bounded by `lock_timeout`) |
| Metrics from metadata tables | durable, restart-proof, auditable counters | scrape cost grows with history; logs are not in Grafana (no Loki) |
| Natural keys, SCD1 | stable keys across rebuilds and versions | no attribute history if the source ever becomes a change feed |
| PostgreSQL (row store) | one engine for constraints, roles, plans and tests | full-history aggregations are slower than on a columnar engine; mitigated by marts |

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

28 things failed or were caught during the build. Each is logged with its root cause and the
change it led to: [docs/what-failed.md](docs/what-failed.md). The ones that changed the design:

* **Real data broke naive assumptions.** The category file starts with a UTF-8 BOM (a naive
  header check reports a false breaking change); zip codes have significant leading zeros;
  `review_id` is not unique. Contracts now declare encoding per file, zips are text, and the
  review grain is `(review_id, order_id)`.
* **Least privilege caught me four times.** No TEMP privilege for staging; no cascade drop for
  migrations; the admin role unable to read dbt output; demoted `*_prev` schemas still readable
  by reporting. Each time the fix was an explicit, minimal grant or a revoke, never a broader
  role.
* **Staging was 5x too slow.** Expanding every record once per check (10 M rows for
  geolocation) was replaced by a boolean prefilter: 15.8 s → 2.9 s, identical output.
* **Observability that looked fine was wrong.** Metrics logged as `"Decimal('0')"` strings,
  third-party lines broke the JSON log file, and a Grafana table hid all flagged records behind
  an empty first query. The last one was only visible by opening the dashboard.
* **Reproducibility leaks.** Grafana downloaded and auto-updated plugins at every start (now
  pinned and baked in); Linux CI would have failed on file ownership invisible on Docker
  Desktop (caught by simulating the runner); the benchmark would have recorded no commit.
* **Tests that were wrong, not the code.** Three times a test expectation was incomplete and
  the system was right: a quarantine cascade, a second legitimately reported rule, and
  date-grained vs timestamp-grained counts (60 vs 63).

## 17. Known Limitations

Consolidated in Phase 14 (production-readiness review). Current list:

* **Single node:** one PostgreSQL instance and one Prefect worker; no replication, no HA.
* **Static snapshot:** Olist v2 ends in 2018 with incomplete edge months (flagged, not hidden).
  Freshness therefore measures publication, not business recency.
* **Full refresh:** rerun cost is close to initial-load cost (section 12).
* **Timestamps have no time zone** in the source; they are treated as Brazilian local time
  (documented assumption).
* **Local-only monitoring extras:** alerts are evaluated but not routed (no Alertmanager); logs
  stay in files (no Loki); Grafana allows anonymous *viewer* access on localhost.
* **CI:** only the `test` job has been rehearsed locally; the workflow has not yet run on GitHub.
* **Benchmark host:** timings come from a memory-constrained laptop and are comparable only with
  each other.

## 18. How to Run

Prerequisites: Docker with Compose v2, and any Python 3 (standard library only) to generate
`.env`. Everything else runs in containers.

```bash
# 1. configuration: random secrets, never committed
python scripts/make_env.py

# 2. database + migrations
docker compose up -d --wait postgres
docker compose up --build migrate

# 3. the whole pipeline, orchestrated (download → ... → publish), once
docker compose run --rm dev olist source fetch      # ~126 MB from Kaggle, verified, read-only
docker compose run --rm dev olist run               # the olist_refresh flow

# 4. operate it as a service + observe it
docker compose up -d prefect-server pipeline-worker metrics-exporter prometheus grafana
docker compose exec pipeline-worker prefect deployment run olist_refresh/olist-refresh
#    Prefect UI http://localhost:4200 · Grafana http://localhost:3000 · Prometheus http://localhost:9090

# 5. consume: business report from the published marts (reporting role)
docker compose run --rm dev python scripts/business_report.py
```

Individual steps are also CLI commands (`olist ingest | stage | transform | warehouse |
rollback-publish | fingerprint | exporter`); `docker compose run --rm dev olist --help` lists
them.

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

**How can fragmented ecommerce operational data be transformed into a trusted, analytics-ready
data warehouse that can be safely refreshed and used for business reporting?**

By treating trust as something the platform *proves on every run* rather than assumes:

1. **Pin the input.** The nine files are checksum-locked; a changed byte stops the run before
   anything is written.
2. **Make the rules explicit.** Every file has a contract, and every rule has a severity.
   Invalid records are never silently dropped: they are quarantined with rule, severity, run
   and reason (0 quarantined, 542 flagged on Olist v2).
3. **Model for the questions asked.** A star schema at the true grains (person, not per-order
   customer id; four facts, no fan-out), with the database itself enforcing keys, and marts
   that answer reporting questions in milliseconds.
4. **Gate publication.** 36 classified tests; a CRITICAL failure blocks the atomic swap, so
   consumers only ever see a complete, validated version, and rollback is one command.
5. **Make refreshes safe and boring.** Reruns are idempotent (byte-identical tables across
   three real-data runs), retries apply only to transient failures, and every run is measured
   and alertable.

What the governed warehouse answers today, generated from the published marts:

<!-- BUSINESS:START -->
Generated by `scripts/business_report.py` from the published marts (reporting role). Full report: [docs/business_metrics.md](docs/business_metrics.md).

| Business question | Answer from the governed warehouse |
|---|---|
| How much was sold? | 99,441 orders, GMV R$ 15,735,527.03, AOV R$ 160.24, 2016-09-04 to 2018-10-17 |
| How reliable is delivery? | median lead time 10.2 days vs 24.4 promised; 6.8% of deliveries late |
| Does lateness matter? | late orders average 2.27 stars vs 4.29 on time; 62.4% of late orders score 1-2 vs 9.3% |
| Do customers come back? | 3.0% of 96,096 customers placed more than one order |
| How concentrated are sellers? | the top 10% of 3,095 sellers earn 67.6% of revenue |
| Which periods are unreliable? | 2016-12, 2018-09, 2018-10: incomplete edge months, flagged automatically |
<!-- BUSINESS:END -->

The most actionable finding is one no single source file contains: **late deliveries cost
satisfaction far more than they cost time**. They are a minority of deliveries, yet most of
them end in a 1-2 star review. It took joining orders, deliveries and reviews at the right
grain, with the right definition of "late", to see it, which is the point of the platform.
