# ADR-0009: Least-privilege database roles

**Status:** Accepted (2026-09-26)

## Decision
| Role | Privileges | Used by |
|------|------------|---------|
| `postgres` (superuser) | bootstrap only | `infra/postgres/init/*.sh` on the container's first start |
| `olist_admin` | owns `meta`, `raw`, `src`; DDL | Alembic migrations |
| `olist_pipeline` | DML on `meta`/`raw`/`src`; `CREATE` on the database (dbt schemas, swap) | ingestion, dbt, Prefect worker |
| `olist_reporting` | `USAGE` + `SELECT` on `warehouse`, `marts` | analysts, BI, benchmark queries |
| `olist_monitor` | `USAGE` + `SELECT` on `meta` | metrics exporter |
| `prefect` | owns the `prefect` database only | Prefect server |

Every password comes from environment variables (`.env`, never committed). The integration test
suite checks the forbidden cases: reporting cannot read `raw` or write anything, and monitor
cannot read `src`.

## Consequences
+ A leaked reporting credential cannot change data or read unpublished data.
− There are more connection strings to manage. They are centralised in
  `olist_platform.config`.
