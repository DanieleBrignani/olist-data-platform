# ADR-0005: Build-then-swap publication of warehouse and marts

**Status:** Accepted (2026-09-26)

## Context
The brief says "Critical failures must prevent publication of downstream marts." Suppose dbt
wrote directly to the schemas that reporting users query. A failed test would then be found
*after* consumers had already seen the bad data, and a half-finished run would leave a mixed
state.

## Decision
dbt builds dimensions and facts into `warehouse_build`, and marts into `marts_build`. Once
`dbt_test` and the quality gate pass, `publish_marts` runs the swap in **one transaction**:

```sql
DROP SCHEMA IF EXISTS warehouse_prev CASCADE;  DROP SCHEMA IF EXISTS marts_prev CASCADE;
ALTER SCHEMA warehouse RENAME TO warehouse_prev; ALTER SCHEMA marts RENAME TO marts_prev;
ALTER SCHEMA warehouse_build RENAME TO warehouse; ALTER SCHEMA marts_build RENAME TO marts;
GRANT USAGE ON SCHEMA warehouse, marts TO olist_reporting;
GRANT SELECT ON ALL TABLES IN SCHEMA warehouse, marts TO olist_reporting;
```

Renaming a schema only changes the catalogue. Readers see either the old warehouse or the new
one, never a mix. `*_prev` is kept for one cycle so a rollback is instant
(`scripts/rollback_publish.py`).

## Consequences
+ A failed gate leaves the published data exactly as it was.
+ Rollback is a metadata operation.
− The rename needs `ACCESS EXCLUSIVE` locks, so long-running reporting queries delay
  publication. `publish_marts` sets `lock_timeout` and treats a timeout as transient, so it
  retries.
− dbt incremental models cannot live in the build schemas, because their state would be swapped
  away. This matches ADR-0006.
