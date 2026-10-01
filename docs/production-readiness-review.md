# Production-readiness review

Date: 2026-09-29. Scope: the whole repository. Every claim below points to a file, a test or a
command that can be re-run; anything not verified is listed as such. This document supersedes
the earlier build-time review.

## Executive summary

The platform is a **reproducible, single-node batch warehouse**. Each of these correctness
properties is backed by a test or a re-runnable command:
* checksum-locked inputs;
* record-level quarantine with lineage;
* a severity-based quality gate in front of an atomic publication;
* change detection that makes reruns cheap and safe;
* failure semantics proven by injection tests;
* a verified backup/restore round-trip.

**All 265 tests pass**, with 93% line coverage. The CI test job passes in a simulated Linux
runner.

It is **not** a highly available production deployment:
* one PostgreSQL instance, no PITR;
* local, manual backups;
* no authentication on the local UIs;
* alerts evaluated but not routed;
* **GitHub Actions has never run**, because the repository has not been pushed.

The gap to production is operational (managed database, secrets, auth, CD), not a
correctness gap in the data path.

This review found and fixed three real defects:
* stale build tables could be published;
* killed runs stayed "running" forever;
* `make benchmark` collided with a running stack.

It also found two tests that encoded outdated behaviour.

## Audit at the start of this review (Phase 1)

| Component | Implementation | Strengths | Weaknesses found | Production risk | Action | Priority |
|-----------|----------------|-----------|------------------|-----------------|--------|----------|
| Raw ingestion | sha256 lock, streaming `COPY`, per-file transaction, lineage | idempotent by file, record-level quarantine | none material | low | keep | DO NOT CHANGE |
| Contracts + staging engine | 32 YAML rules → set-based SQL, cascade FK quarantine | one severity model end to end; measured thresholds | none material | low | keep | DO NOT CHANGE |
| Rerun cost | checksum skip, then full rebuild of `src` + dbt | deterministic | an unchanged rerun cost 76 s of an 89 s initial load; logic changes not tracked as inputs | wasted compute; a stale warehouse after logic-only changes if skipping were naive | input fingerprint (snapshots + transformation code) → skip or rebuild | HIGH |
| Build/publish | dbt into `*_build`, atomic swap, `*_prev` rollback | consumers never see partial builds | build schemas survived failed runs, so leftovers from removed models or killed runs could be published | wrong tables in production | empty build schemas before every build | BLOCKER |
| Run registry | `meta.pipeline_runs` with failure handler | failing task recorded | a killed process left `running` rows forever | phantom in-progress runs, misleading metrics | close runs older than 3 h as `abandoned` | HIGH |
| dbt models | 33 models, enforced core contracts, 36 tests | real PK/FK in the database | staging/intermediate undocumented; intermediate grains untested | fan-out bugs visible only as inflated marts | docs + grain tests + generated lineage | MEDIUM |
| Indexes | 5 warehouse indexes | – | only 1 of 5 measured | unjustified write/storage cost | EXPLAIN ANALYZE each; drop without evidence | MEDIUM |
| Developer UX | raw `docker compose` commands | explicit | no single entry point; long commands | onboarding friction, typos | Makefile | MEDIUM |
| Backup | none | the warehouse is rebuildable from raw | `meta` history unrecoverable | audit trail lost with the volume | `make backup/restore` + round-trip proof | HIGH |
| CI | 4 jobs, actionlint-clean | covers lint → real data | never run on GitHub; Makefile not exercised | first push may fail | run via make targets; add a backup round-trip; simulate on Linux | HIGH |
| Observability | metrics from `meta`, dashboard, alert rules | restart-proof counters | alerts not routed | missed failures in a shared deployment | document; out of scope locally | LOW |
| Security | per-role secrets, least privilege, localhost ports | clean secret scans | no dependency or image scan performed | unknown CVEs | pip-audit + Trivy, documented | MEDIUM |
| Documentation | 20-section README, ADRs, what-failed log | traceable numbers | phase-by-phase artefacts, stale docstrings, `.gitkeep` noise | reviewer confusion | restructure README; clean up | LOW |
| Orchestration choice | Prefect 3 | error-type retries, testable flow | – | – | keep (no Airflow migration) | DO NOT CHANGE |

## Architecture

Diagrams for the data flow, control flow and observability plus CI:
[architecture.md](architecture.md). Generated model lineage: [lineage.md](lineage.md).
A single PostgreSQL instance holds `meta`, `raw`, `src`, the dbt layers and the published
`warehouse`/`marts`. Prefect orchestrates, a metrics exporter derives metrics from `meta`,
and Prometheus/Grafana monitor it. Deliberately single-node (ADR-0001).

## What is genuinely strong

1. **Gated atomic publication:** a failed gate, a dbt error or a crash leaves consumers on
   the previous complete version; rollback is one command.
2. **Record-level accountability:** `raw records = sum(src.source_record_count) + distinct
   quarantined`, asserted on real data. Nothing disappears silently.
3. **Idempotency at every layer:** file checksums, snapshot-replace staging, input
   fingerprints over data *and* code, and byte-identical republication (3 real-data runs,
   plus every benchmark repetition).
4. **Failure semantics as tests:** 28 failure-injection tests, including a killed run, a
   broken dbt model, database loss, a tampered file and a failed gate.
5. **Evidence discipline:** benchmark and business blocks are generated and test-guarded.
   Index decisions carry EXPLAIN plans, and README links are checked by a test.

## What has been verified

| Verified | How |
|----------|-----|
| All tests pass | 265/265: 249 in a Linux container on a clean export (`make test-ci`), 14 real-data tests on the isolated test database, and the final unit suite (158) |
| CI `test` job logic | executed step by step on a clean export via make, as uid 1001, including `make verify-backup` |
| Backup/restore | `make verify-backup`: 41 tables identical after damage + restore |
| Performance | `make benchmark` in an isolated project, 3 repetitions from empty ([benchmark.md](benchmark.md)) |
| Index value | EXPLAIN ANALYZE with and without each index ([query_plans.md](query_plans.md)) |
| Security | gitleaks (full history), `.env` value scan, pip-audit, Trivy ([security.md](security.md)) |
| Mermaid diagrams | rendered with Mermaid 11 |

## What has NOT been verified

* **GitHub Actions on GitHub-hosted runners**: never run, because the repository has not been
  pushed. The `real-data` and `images` jobs have not been simulated as jobs; their commands
  were run individually.
* Behaviour under **concurrent consumers** during publication, beyond the `lock_timeout`
  unit of behaviour.
* **Long-running operation**: metadata growth, Prometheus retention, and weeks of scheduled
  runs.
* **Restore of the full 734 MB warehouse** (the round-trip was run on the test database).
* **Late data, schema evolution and deletes in a real feed**: only with synthetic versions.
* **Alert delivery**: there is no receiver.

## Data integrity

* PK/FK/CHECK constraints are enforced in `src` and in the warehouse (9 PK, 20 FK).
* Reconciliation tests check `src` → warehouse to the cent, and marts → facts.
* Quarantine keeps the original text with its file and row reference.
* Grain tests run on every intermediate model.

Evidence: `tests/integration/test_staging.py`, `dbt/tests/assert_*`,
[data_quality.md](data_quality.md).

## Idempotency

* Files are identified by sha256, so a reload replaces rows and never appends.
* `src` is rebuilt per snapshot in one transaction.
* dbt builds into emptied build schemas and publishes by swap.
* The input fingerprint skips unchanged inputs.

Evidence: `tests/e2e/test_real_pipeline.py`, `tests/e2e/test_incremental.py`,
[incremental.md](incremental.md).

## Data quality

* Three checkpoints (files, records, models) share one severity vocabulary.
* 32 contract rules and 51 dbt tests.
* The gate blocks on CRITICAL, and on ERROR above tolerance; unclassified tests count as
  CRITICAL.
* On Olist v2: 0 quarantined, 542 flagged, 6 warnings, PASS.

[data_quality.md](data_quality.md).

## Data modelling

* A star schema with 5 dimensions and 4 facts, at declared and enforced grains.
* The customer dimension is the person, not the per-order id.
* Natural keys, no fabricated SCD2 history.
* Seven marts, reconciled with the facts.

[data_model.md](data_model.md), [lineage.md](lineage.md).

## Orchestration

* An 11-task Prefect flow with per-task timeouts and a 2-hour flow timeout.
* Retries only for transient error types, with backoff and jitter.
* No caching; one run at a time.
* Killed runs are detected and closed.

[orchestration.md](orchestration.md), [failure-recovery.md](failure-recovery.md).

## Observability

* JSON logs with run, task and row fields.
* Seven pipeline metrics derived from `meta`; freshness from a recording rule.
* A provisioned Grafana dashboard (a drift test ties panels to exported metrics).
* Four alert rules, evaluated but **not routed**.

[observability.md](observability.md).

## Testing

| Category | Count |
|----------|------:|
| Unit | 158 |
| Integration (synthetic) | 78 |
| End-to-end (synthetic) | 15 |
| Real data | 14 |
| **Total** | **265** |

Of these, 28 are failure-injection tests. Line coverage is 93% (lowest: the CLI wrapper at
57%, and `orchestration/serve.py`, which only starts the server). A full run takes about
25 min. [testing.md](testing.md).

## CI/CD

Four jobs: lint, test, real-data and images. The test job runs through `make` targets and
includes reversible migrations and a backup round-trip. **Status: simulated locally only;
never run on GitHub.** There is no CD, because there is no target environment.
[ci.md](ci.md).

## Performance

Measured by `make benchmark` at commit `0ae2335` (median of 3 repetitions from an empty
database, [benchmark.md](benchmark.md)):
* initial load: 109.2 s;
* **unchanged rerun: 2.1 s**, against a forced rebuild of the same inputs: 94.6 s;
* ingestion: 54k rows/s; staging: 37k rows/s;
* dbt: 27.6 s build + 6.5 s test (51 tests);
* 734 MB database;
* reporting queries: 0.3 ms from a mart against 20.5 ms from the facts.

All five warehouse indexes are kept: 7×–400× faster lookups for 0.9–3.1 s of build time.

The initial load is slower than the earlier 89.1 s measurement. It includes 15 more dbt tests,
and the main stack ran alongside on a memory-constrained laptop, so the spreads are wide.

## Security

* No secret in git history (gitleaks, `.env` value scan).
* No known vulnerabilities in the 147 locked Python packages.
* 0 fixable HIGH/CRITICAL findings in the runtime image.
* Five least-privilege roles; every port bound to localhost.

Gaps: no authentication on Prefect or Grafana, no secrets manager, scans not scheduled.
[security.md](security.md).

## Reproducibility

* Locked dependencies (`uv.lock`) and pinned images and plugins.
* A committed checksum lock for the data.
* `.env` generated with random secrets.
* A Makefile for every workflow.

The fresh-clone Quick Start test has not yet been run for this revision (see below).

## Recovery

* **Consumer-facing failures:** the previous version stays published, and a rollback is one
  command.
* **Volume loss:** the warehouse is rebuilt from raw files in under 2 minutes, and `meta`
  comes back from `make backup`.
* **No PITR:** writes after the last dump are lost.

[failure-recovery.md](failure-recovery.md), [backup-and-recovery.md](backup-and-recovery.md),
[runbook.md](runbook.md).

## Known limitations

See README section 15. The main ones:
* single node;
* never run on GitHub;
* static source;
* a full rebuild whenever any input changes;
* unauthenticated local UIs;
* no metadata retention.

## Risk register

| Risk | Likelihood | Impact | In place | Next step |
|------|-----------|--------|----------|-----------|
| PostgreSQL volume loss | low (local) | loss of `meta` history since the last dump | warehouse rebuildable from locked raw files; `make backup` with a verified restore | scheduled off-host backups or a managed DB with PITR |
| First GitHub run fails | medium | CI red on push | Linux simulation of the test job; actionlint | push and fix runner-specific issues |
| Kaggle unavailable | low | new environments cannot bootstrap | any local copy matching the lock is accepted; CI caches by lock hash | mirror the archive |
| Silent bad data | low | wrong decisions | 32 contract rules, 51 classified dbt tests, reconciliation, gate | row-count anomaly checks |
| Long report blocks publication | medium | delayed refresh | `lock_timeout` → transient → bounded retries | – |
| Credential leak | low | data exposure | per-role secrets, read-only reporting, clean history | secrets manager, rotation |
| Metadata growth | certain over time | slower scrapes | indexes on hot paths | retention policy |

## Recommended next architectural step

**Per-table incremental staging.** The fingerprint already identifies which source snapshots
changed. Rebuilding only those `src` tables, and the dbt models downstream of them
(`dbt build --select state:modified+` equivalent), would cut the cost of a real change
without giving up the build-then-swap guarantee. It is the one change that would matter for
a growing dataset. It should come *after* moving to a managed PostgreSQL with PITR, which is
the operational prerequisite for anything shared.

## Fresh-clone test

**Not yet performed for this revision.** The previous review ran a fresh clone end to end
(`--no-cache` images, real download, orchestrated run, published tables identical to the main
stack), but the Makefile-based Quick Start has not been run on a fresh clone. Note for that
test: `docker-compose.yml` sets `name: olist-platform`, so a second checkout on the same
machine shares the main stack's volumes unless `COMPOSE_PROJECT_NAME` is set.

## Final evidence table

| Claim | Evidence | File / test | Verified? |
|-------|----------|-------------|-----------|
| Raw inputs are immutable and pinned | sha256 lock verified before any write | `test_file_changed_after_lock_fails_manifest_check`, `test_tampered_source_fails_at_verify_manifest_before_any_write` | yes |
| Ingestion is idempotent | rerun no-op; forced reload replaces | `test_load_then_rerun_is_a_recorded_no_op`, `test_force_reload_replaces_rows_without_duplicates` | yes |
| Every record is accounted for | loaded + rejected = locked rows; reconciliation invariant | `test_raw_files_to_marts_accounts_for_every_record` | yes (real data) |
| Unchanged inputs are not rebuilt | skip decision + unchanged tables | `test_first_run_loads_everything_and_identical_rerun_changes_nothing`; benchmark rerun | yes |
| Logic changes force a rebuild | transformation code in the fingerprint | `test_changed_transformation_logic_triggers_rebuild` | yes |
| Rebuilds are deterministic | byte-identical published tables | `test_every_run_publishes_byte_identical_tables`; benchmark | yes (real data) |
| Bad data never reaches consumers | gate FAIL → no publication | `test_injected_invalid_source_condition_blocks_publication` | yes |
| Stale build tables cannot be published | build schemas emptied | `test_killed_run_is_closed_and_leftovers_are_never_published` | yes |
| Only transient errors are retried | retry condition by type | `tests/integration/test_orchestration_retries.py`, `test_dbt_model_failure_publishes_nothing_and_is_not_retried` | yes |
| Rollback works | swap back + next run rebuilds | `test_rollback_restores_previous_publication_and_revokes_prev`, `test_after_rollback_the_next_run_rebuilds_the_current_inputs` | yes |
| Backups restore correctly | round-trip identical | `make verify-backup` (41 tables) | yes (test DB) |
| Least privilege | role boundary tests | `tests/integration/test_database_roles.py` | yes |
| Every warehouse index pays off | EXPLAIN ANALYZE with/without | [query_plans.md](query_plans.md) W1–W5 | yes |
| No secrets in git | gitleaks + value scan | [security.md](security.md) | yes |
| No known dependency CVEs | pip-audit, Trivy | [security.md](security.md) | yes (at audit date) |
| Business insights come from governed data | generated from marts by the reporting role | `scripts/business_report.py`, [business_metrics.md](business_metrics.md) | yes |
| CI passes on GitHub | – | `.github/workflows/ci.yml` | **no: never run on GitHub** |
| Fresh clone works with the Quick Start | Makefile targets verified individually on Linux (`help`, `lint`, `test-ci`, `verify-backup`, `benchmark`) | README section 13 | **partly: not run end to end on a fresh clone** |
