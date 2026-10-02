# Orchestration

Design: [ADR-0003](adr/0003-prefect-orchestration.md). Code: `orchestration/`.

## The flow

`olist_refresh` (Prefect 3) is **one** pipeline run in `meta.pipeline_runs`. If a step fails,
the run is marked `failed` with the name of the failing task and its `error_type`.

```
download_or_locate_source → verify_manifest → validate_source → ingest_raw → load_staging
    → dbt_build → dbt_test → quality_gate → publish_marts → publish_metrics
```

Every task calls the same functions as the CLI and the tests. Orchestration adds policy, not
logic.

| Task | Timeout | Retries | Why |
|------|--------:|---------|-----|
| download_or_locate_source | 15 min | 3, exponential backoff 10 s → 20 s → 40 s, ±50% jitter | network/HTTP 5xx/429 are transient; HTTP 4xx and corrupt archives are not |
| verify_manifest | 10 min | **0** | a checksum mismatch is deterministic |
| validate_source | 10 min | **0** | a breaking schema change is deterministic |
| ingest_raw | 30 min | 2 (transient only) | DB connection drops; contract violations are never retried |
| load_staging | 30 min | 2 (transient only) | same |
| dbt_build | 30 min | 1 (transient only) | a model error is deterministic; a DB restart is not |
| dbt_test | 30 min | 1 (transient only) | test *failures* are data, judged by the gate, never retried |
| quality_gate | 5 min | **0** | the same data gives the same verdict |
| publish_marts | 5 min | 2 (transient only) | a lock timeout while a long report holds the schema is transient |
| publish_metrics | 2 min | 2 (transient only) | reads metadata only |

The whole flow times out at 2 hours.

### What "transient" means (`orchestration/policies.py`)

A task retries only when `retry_condition_fn` classifies the failure as transient:
`TransientError`, SQLAlchemy/psycopg `OperationalError`, `ConnectionError`, `TimeoutError`.
Everything else fails at once, even on a task configured with retries:
`ManifestMismatchError`, `DataContractError`, `QualityGateError`, `DbtError` and programming
errors. This is proven on a real Prefect engine in `tests/integration/test_orchestration_retries.py`:
* a deterministic error runs exactly once;
* a transient error is retried until it succeeds, and gives up after the bounded number of
  attempts.

### Failure callbacks

* **Task `on_failure`:** a structured `task_failed` event with `error_type`, the number of
  attempts, and whether the error was retryable.
* **Flow `on_failure` / `on_crashed`:** a `flow_failed` event with the final state and message.
* `tracked_run` records the failed task and `error_type` in `meta.pipeline_runs`. The metrics
  exporter derives `pipeline_failures_total` from that table.

### Caching is disabled on purpose

Every task uses `cache_policy=NO_CACHE`. Pipeline steps have side effects, so a step must
never be skipped because its inputs look identical to a previous run's. Idempotency comes
from the data layer (checksums, atomic rebuilds, schema swap), not from orchestrator caching.

## Runtime

| Service | What it does |
|---------|--------------|
| `prefect-server` | API + UI on http://localhost:4200. Metadata in the `prefect` database, isolated from the warehouse. |
| `pipeline-worker` | `orchestration/serve.py`: registers the `olist-refresh` deployment and executes its runs, at most **one at a time** (`limit=1`). That limit only covers deployment runs: the guarantee against interleaving (CLI steps, other workers) is the database-level pipeline lock held by every writing operation (`src/olist_platform/database/locking.py`, [failure-recovery.md](failure-recovery.md) scenario 11). The optional `OLIST_REFRESH_CRON` sets a schedule. |

Trigger a run:

```bash
docker compose exec pipeline-worker prefect deployment run olist_refresh/olist-refresh
```

or run the flow once without the server (ephemeral Prefect):

```bash
docker compose run --rm dev olist run
```

## Known limitations

* Prefect enforces timeouts on synchronous tasks cooperatively. A long blocking call inside
  a C extension (for example one very long SQL statement) is not interrupted until it returns.
  Database-side limits complement it: the reporting and monitor roles have a
  `statement_timeout`, and `publish_marts` uses `lock_timeout`.
* A single worker process runs the flow in-process. Scaling out would mean a work pool with
  several workers, which a single-snapshot dataset does not need.
