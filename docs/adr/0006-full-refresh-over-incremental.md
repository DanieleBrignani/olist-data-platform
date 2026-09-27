# ADR-0006: Full refresh for dbt models; incrementality only at file ingestion

**Status:** Accepted (2026-09-26)

## Context
The source is a static, versioned snapshot of about 100k orders. Records can be *updated*
between versions (order status, delivery dates), so an append-only incremental strategy would
be wrong. A merge strategy would need reliable `updated_at` columns, and the dataset has none.

## Decision
* Every dbt model is either a `view` (stg/int) or a `table` (warehouse/marts), fully refreshed
  on every run.
* The only incremental behaviour is at the **file** level: a file whose checksum is already
  loaded is skipped (ADR-0008).
* Revisit this if (a) the source becomes a change feed, or (b) the measured `dbt build` time
  becomes a bottleneck. The benchmark (Phase 12) records that time.

## Consequences
+ Results are deterministic, and idempotency is structural rather than dependent on merge logic.
+ It works with the build-then-swap publication (ADR-0005).
− Cost grows linearly with total volume. That is acceptable at this volume and is documented
  as a trade-off.
