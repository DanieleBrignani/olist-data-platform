# ADR-0001: PostgreSQL as the single warehouse engine, schema per layer

**Status:** Accepted (2026-09-26)

## Context
The full dataset is about 126 MB of CSV (Kaggle reports 126,186,995 bytes). Around 1 M of its
rows are geolocation; the exact row counts are recorded in the checksum manifest during ingestion.
The platform must run on a laptop with `docker compose up` and in GitHub Actions. It must also
show relational engineering: PK/FK/CHECK constraints, indexes and `EXPLAIN ANALYZE`.

## Decision
Use a single PostgreSQL 16 instance. Each layer gets its own **schema**
(`meta`, `raw`, `src`, `stg`, `int`, `warehouse(_build)`, `marts(_build)`) rather than its own
database. That way dbt `ref()`s, FKs and the atomic publish swap (ADR-0005) all work inside one
transaction. Prefect's own metadata lives in a *separate database* (`prefect`) on the same
instance, so the orchestrator can never read or lock warehouse objects.

## Consequences
+ Everything reproduces locally and in CI from an official image.
+ Real constraints and query plans can be shown and tested.
− Storage is row-oriented, so analytical scans are slower than on a columnar engine. That is
  acceptable at this volume. The benchmark measures it instead of assuming it.
− One instance is a single point of failure. That is acceptable for a portfolio platform and is
  listed under Known Limitations.

## Alternatives rejected
* **DuckDB**: excellent for analytics, but it has no multi-role security model and no server
  for concurrent access by the orchestrator, the exporter and BI tools.
* **A cloud warehouse (BigQuery/Snowflake)**: a reviewer cannot reproduce it without an
  account, and the project requirements rule out architecture that cannot run locally.
