# Production-readiness review

Review period: 2026-09-29 to 2026-10-02. It records what was checked, what was found and
fixed, and what remains unverified. Design and behaviour are described in the documents it
links to; they are not repeated here.

## Summary

The data path behaves the way a production batch pipeline should:
* inputs are pinned;
* invalid records are quarantined, not dropped;
* nothing is published without a passing quality gate;
* publication is atomic;
* reruns are idempotent;
* writers are serialised.

Each of these properties has tests (see the evidence table below).

The operations do not meet that bar:
* one PostgreSQL instance with no point-in-time recovery, and manual local backups;
* no authentication on Prefect or Grafana;
* alerts that are not routed anywhere;
* no deployment pipeline (CI exists, CD does not).

Closing that gap means infrastructure work (managed database, secrets, authentication,
deployment), not changes to the data path.

## Defects found and fixed during the review

| Defect | Fix | Test |
|--------|-----|------|
| Tables left in `*_build` by a removed model or a killed run could be published | build schemas are dropped before every build | `test_killed_run_is_closed_and_leftovers_are_never_published` |
| A killed run stayed `running` forever | the next run closes runs older than 3 h as `abandoned` | same |
| A CLI step could interleave with a deployment run (the deployment's one-run limit does not cover it) and publish half-built schemas | PostgreSQL advisory lock held by every writing operation | `tests/integration/test_pipeline_lock.py` |
| A change to the quality-gate code was skipped by the unchanged-input shortcut | gate and validation code added to the change-detection fingerprint | `test_a_stricter_policy_that_rejects_published_data_keeps_it_but_never_certifies_it` |
| `make benchmark` failed when the main stack was running (same host port) | separate port for the benchmark project | run of `make benchmark` |
| The first GitHub Actions run failed: `astral-sh/setup-uv@v10` does not exist as a tag | pinned to `v10.2.0` | CI run #3 |
| `make pipeline` timed out on a fresh clone starting an ephemeral Prefect server | it uses the stack's Prefect server | fresh-clone test |

Two end-to-end tests also still encoded the earlier "every run rebuilds" behaviour; they were
updated. Root causes for all of these: [what-failed.md](what-failed.md).

## Audit at the start of the review

| Component | Finding | Risk | Action | Priority |
|-----------|---------|------|--------|----------|
| Raw ingestion | idempotent by file checksum, per-file transactions, lineage | low | keep | — |
| Contracts and staging | one severity model, thresholds measured on real data | low | keep | — |
| Rerun cost | an unchanged rerun cost 76 s of an 89 s initial load | wasted compute | input fingerprint: skip or rebuild | high |
| Build and publish | build schemas survived failed runs | stale tables published | drop build schemas before each build | blocker |
| Run registry | killed processes left `running` rows | misleading metrics | close abandoned runs | high |
| dbt models | staging and intermediate models undocumented; intermediate grains untested | fan-out errors visible only in marts | docs, grain tests, generated lineage | medium |
| Indexes | 1 of 5 warehouse indexes measured | unjustified cost | `EXPLAIN ANALYZE` each one | medium |
| Developer commands | long `docker compose` commands, no single entry point | onboarding errors | Makefile | medium |
| Backup | none; `meta` history not reproducible from raw files | audit trail loss | `make backup` / `restore` with a round-trip check | high |
| CI | never run on GitHub; Makefile not exercised | first push may fail | run through make; simulate on Linux; push | high |
| Security | no dependency or image scan | unknown vulnerabilities | pip-audit, Trivy | medium |
| Observability | alerts not routed | missed failures in shared use | documented as a limitation | low |

## What was verified, and when

| Check | Result | When / version |
|-------|--------|----------------|
| Full test suite | 283 passed (269 synthetic-data tests with 93% line coverage, then 14 real-data tests) | 2026-10-01, working tree with the locking and change-detection changes |
| Unit suite, including the new documentation checks | 163 passed | 2026-10-02 |
| GitHub Actions | all four jobs passed in 22 min 33 s, including the real-data pipeline | 2026-10-01, run #3, commit `18f6d5a`, in the original repository ([ci.md](ci.md)); current runs are under the Actions tab |
| Fresh clone, Quick Start end to end | passed after one fix; 265 tests of that version passed; published tables identical to an existing stack | 2026-10-01, commit `8661bad` (record below) |
| Benchmark | 3 repetitions from an empty database | 2026-09-29, commit `0ae2335` ([benchmark.md](benchmark.md)) |
| Backup and restore | 41 tables identical after damage and restore, on the test database | 2026-09-29 ([backup-and-recovery.md](backup-and-recovery.md)) |
| Index value | each warehouse index measured with and without | 2026-09-29 ([query_plans.md](query_plans.md)) |
| Security scans | gitleaks over the full history, `.env` value search, pip-audit, Trivy: no findings | 2026-09-29 ([security.md](security.md)) |

### Commit identifiers

The repository history was rewritten twice after these results were recorded (commit
messages were edited, and two files were removed from every commit), and the GitHub
repository was then recreated from the rewritten history. The file contents of each commit
are otherwise unchanged, but the identifiers are not, and the first Actions runs are no
longer on GitHub. Identifiers quoted in the documentation are the ones the results were
recorded against. Their equivalents in the current history:

| Recorded as | Current commit | Subject |
|-------------|----------------|---------|
| `dee7d90` | `52c7984` | Production data platform for the Olist Brazilian e-commerce dataset (first commit) |
| `0ae2335` | `d4c570a` | fix: run the benchmark project on its own PostgreSQL host port |
| `18f6d5a` | `e15ae46` | ci: pin astral-sh/setup-uv to v10.2.0 |
| `e315f2e` | `1af4626` | docs: record the first green GitHub Actions run; add CI badge |
| `8661bad` | `15a9ba0` | fix: run make pipeline against the stack's Prefect server |
| `04a85ec` | `818fa6c` | docs: record the fresh-clone Quick Start test |

## What was not verified

* Restoring the full 734 MB warehouse (the round-trip ran on the test database).
* Long-running operation: metadata growth, Prometheus retention, weeks of scheduled runs.
* Publication under concurrent heavy reporting load (only the `lock_timeout` path is tested).
* Late-arriving data, schema evolution and deletes in a real feed (synthetic versions only).
* Alert delivery (there is no receiver).

## Risk register

| Risk | Likelihood | Impact | In place | Next step |
|------|-----------|--------|----------|-----------|
| PostgreSQL volume loss | low (local) | `meta` history since the last dump | warehouse rebuildable from locked raw files; `make backup` with a verified restore | scheduled off-host backups or a managed database with PITR |
| CI breaks after an upstream change | medium | red builds | exact pin for `setup-uv`; other actions on major tags; locked Python dependencies | pin actions by commit SHA; Dependabot |
| Kaggle unavailable | low | new environments cannot bootstrap | any local copy matching the lock is accepted; CI caches by lock hash | mirror the archive |
| Silent bad data | low | wrong decisions | 32 contract rules, 51 classified dbt tests, reconciliation, quality gate | row-count anomaly checks |
| Long report blocks publication | medium | delayed refresh | `lock_timeout`, classified transient, bounded retries | — |
| Hung writer keeps the pipeline lock | low | refreshes fail with `pipeline_busy` | holder's pid in the error; 2 h flow timeout | operator alert on repeated `pipeline_busy` |
| Credential leak | low | data exposure | per-role passwords, read-only reporting, clean history | secrets manager, rotation |
| Metadata growth | certain over time | slower metric scrapes | indexes on hot paths | retention policy |

## Recommended next step

Rebuild only the `src` tables whose source snapshot changed, and the dbt models downstream of
them, while keeping the build-then-swap publication. The fingerprint already knows which
snapshots changed. It is worth doing only after moving to a managed PostgreSQL with
point-in-time recovery, which any shared deployment needs first.

## Fresh-clone test

Run on 2026-10-01: `git clone` of the GitHub repository (commit `e315f2e`, then `8661bad`
after the fix below) into an empty directory, then the README quick start, using GNU make
in a Linux container (`docker:28-cli`) against the host Docker engine.

| Step | Result | Time |
|------|--------|-----:|
| `git clone` | 208 files, no data, no `.env` | 6 s |
| `make setup` | `.env` with 7 generated passwords; all images built, dependencies installed from scratch | 9 min 21 s |
| `make up` | 6 services healthy, migrations applied | 1 min 1 s |
| `make pipeline`, first attempt | failed: dataset downloaded and verified, then `olist run` timed out starting an ephemeral Prefect server | 1 min 44 s |
| fix `8661bad`, `git pull`, `make pipeline` | 1,550,922 rows loaded, 0 rejected, 542 flagged, gate PASS, 16 tables / 667,884 rows published | 2 min 38 s |
| `make pipeline` again | skipped by change detection (`inputs_unchanged`) | 29 s |
| `make report` | same business results as the existing stack | 9 s |
| `make test` | 265 passed, 0 failed, 0 skipped (including the 14 real-data tests) | 18 min |
| `make down` | stopped, data kept | 7 s |
| published tables vs the existing stack (`olist fingerprint`) | all 16 identical (row count and content md5) | — |

Differences from a newcomer's run:
* The test used its own compose project (`olistfresh`) and image prefix, so it could not
  touch the existing stack. `docker-compose.yml` sets `name: olist-platform`, so two
  checkouts on one machine share volumes unless `COMPOSE_PROJECT_NAME` differs.
* The existing stack was stopped to free the default ports.
* make ran as root inside the container (`DEV_UID` 0); the CI job covers a non-root uid.
* Base-image layers were cached; dependency layers were rebuilt.

## Evidence table

| Claim | Evidence | Verified |
|-------|----------|----------|
| Raw inputs are pinned | `test_file_changed_after_lock_fails_manifest_check`, `test_tampered_source_fails_at_verify_manifest_before_any_write` | yes |
| Ingestion is idempotent | `test_load_then_rerun_is_a_recorded_no_op`, `test_force_reload_replaces_rows_without_duplicates` | yes |
| Every record is accounted for | `test_raw_files_to_marts_accounts_for_every_record` | yes, real data |
| Unchanged inputs are not rebuilt | `test_first_run_loads_everything_and_identical_rerun_changes_nothing`; benchmark rerun | yes |
| Logic or gate-policy changes force a rebuild | `test_changed_transformation_logic_triggers_rebuild`, `test_a_policy_only_change_forces_a_new_gate_decision_on_unchanged_data` | yes |
| Rebuilds are deterministic | `test_every_run_publishes_byte_identical_tables`; benchmark | yes, real data |
| Bad data is not published | `test_injected_invalid_source_condition_blocks_publication` | yes |
| Writers never interleave | `tests/integration/test_pipeline_lock.py`, `test_a_concurrent_operation_blocks_the_whole_flow_and_nothing_is_published` | yes |
| Only transient errors are retried | `tests/integration/test_orchestration_retries.py`, `test_dbt_model_failure_publishes_nothing_and_is_not_retried` | yes |
| Rollback works | `test_rollback_restores_previous_publication_and_revokes_prev`, `test_after_rollback_the_next_run_rebuilds_the_current_inputs` | yes |
| Backups restore | `make verify-backup` | yes, test database |
| Roles enforce least privilege | `tests/integration/test_database_roles.py` | yes |
| CI passes on GitHub | run #3, commit `18f6d5a` (original repository); current status: README badge | yes, at that commit |
| The quick start works on a fresh clone | record above | yes, at commit `8661bad`, after one fix |
