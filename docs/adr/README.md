# Architecture Decision Records

Format: context → decision → consequences → alternatives rejected. An accepted ADR is never
edited except to change its status; a new ADR supersedes it.

| ADR | Title | Status |
|-----|-------|--------|
| [0001](0001-postgresql-single-warehouse.md) | PostgreSQL as the single warehouse engine, schema per layer | Accepted |
| [0002](0002-immutable-raw-layer-and-manifest-lock.md) | Immutable raw layer pinned by a committed manifest lock | Accepted |
| [0003](0003-prefect-orchestration.md) | Prefect 3 for orchestration; retries decided by error type | Accepted |
| [0004](0004-data-quality-layers-and-severity.md) | Layered data quality with WARNING / ERROR / CRITICAL severities | Accepted |
| [0005](0005-blue-green-publication.md) | Build-then-swap publication of warehouse and marts | Accepted |
| [0006](0006-full-refresh-over-incremental.md) | Full refresh for dbt models; incrementality only at file ingestion | Accepted |
| [0007](0007-metrics-from-metadata-tables.md) | Prometheus metrics derived from metadata tables, not Pushgateway | Accepted |
| [0008](0008-idempotent-ingestion-by-checksum.md) | Idempotent ingestion keyed by file checksum | Accepted |
| [0009](0009-least-privilege-roles.md) | Least-privilege database roles | Accepted |
| [0010](0010-no-bi-tool-in-core-stack.md) | No BI tool in the core stack | Accepted |
| [0011](0011-natural-keys-no-scd2.md) | Stable source identifiers as dimension keys; no SCD2 | Accepted |
