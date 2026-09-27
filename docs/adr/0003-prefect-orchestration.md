# ADR-0003: Prefect 3 for orchestration; retries decided by error type

**Status:** Accepted (2026-09-26)

## Context
The orchestrator must provide:
* a DAG with timeouts;
* bounded retries with exponential backoff for transient errors;
* **no retries for deterministic data errors**;
* failure callbacks and a UI;
* a way to run the *same* flow in CI end-to-end tests without standing up a scheduler.

The local Docker host has 8 GB of RAM shared by every service.

## Decision
Use **Prefect 3**:
* One task decorator expresses the whole retry policy:
  `@task(retries=N, retry_delay_seconds=exponential_backoff(...), retry_jitter_factor=...,
  retry_condition_fn=is_transient, timeout_seconds=...)`. The transient/deterministic split is
  implemented once, in `olist_platform.errors`: `TransientError` is retried,
  `DataContractError` and `QualityGateError` are not.
* `on_failure` and `on_crashed` hooks record the failure in `meta.pipeline_runs` and emit a
  structured log event.
* The flow is plain Python, so `tests/e2e` calls it directly against an ephemeral Prefect API.
  CI therefore tests the real orchestration code, not a mock.
* Runtime: `prefect-server` (API + UI, metadata in the Postgres database `prefect`) and a
  `pipeline-worker` container running `flow.serve()`.

## Consequences
+ Retry semantics are explicit, typed and unit-testable.
+ The footprint is smaller than Airflow's, which needs a separate scheduler, webserver,
  triggerer and metadata-DB init.
− Airflow appears more often in job descriptions. The concepts (DAG, retries, SLAs, callbacks)
  map 1:1, and the README documents the mapping.
− Prefect's server is one more stateful service. Its database is isolated from the warehouse.

## Alternatives rejected
* **Airflow 2/3**: a heavier local footprint. Running a DAG in CI needs `airflow dags test` plus
  a metadata DB, and per-exception retry conditions need custom operators.
* **Cron plus a shell script**: no retries, timeouts, UI or run history.
