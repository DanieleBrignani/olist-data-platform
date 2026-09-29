# Backup and recovery

The platform runs on one PostgreSQL instance with Docker named volumes. This page says what can be rebuilt,
what would be lost, and how to back it up and restore it.

## What lives where

| Data | Location | Reproducible from the source files? |
|------|----------|-------------------------------------|
| Source CSVs | `data/raw/olist/v2` (host, read-only mount), checksums in `data_contracts/manifest.lock.json` | re-download with `olist source fetch`; the lock file rejects a changed file |
| `raw`, `src`, `stg`/`int`, `warehouse`, `marts` | database `olist_dw`, volume `pgdata` | **yes**: `make pipeline` rebuilds them deterministically (real-data idempotency tests show identical tables) |
| `meta` (run history, events, rejected records, quality results, publications) | `olist_dw`, volume `pgdata` | **no**: this is audit history, and a rebuild only starts a new history |
| `*_prev` schemas (rollback target) | `olist_dw` | no, but only needed for `olist rollback-publish` |
| Prefect state (flow runs, deployments) | database `prefect`, same volume | no: deployments are re-registered at start-up by `orchestration/serve.py`; run history is lost |
| Prometheus time series (30-day retention) | volume `promdata` | no: current values are re-derived from `meta` by the exporter, history is lost |
| Grafana | volume `grafanadata` | dashboards and datasources are provisioned from `monitoring/grafana` (git); only UI-made changes are lost |

`make down` keeps every volume. Only `make reset` (`docker compose down -v`) deletes them.

## Commands

```bash
make backup                                   # pg_dump -Fc olist_dw -> backups/olist_dw_<UTC>.dump (verified with pg_restore --list)
make restore FILE=backups/olist_dw_<UTC>.dump # pg_restore --clean --single-transaction into olist_dw
make verify-backup DB=olist_dw_test           # round-trip proof: back up, damage, restore, compare
make backup DB=prefect                        # the Prefect database, if its run history matters
```

`pg_dump`/`pg_restore` run **inside** the postgres container (`scripts/db_backup.sh`,
`scripts/db_restore.sh`), so no client tools are needed on the host. Dumps go to `backups/`
(git-ignored). The restore runs as a single transaction: if it fails, the database is left as it
was.

## Evidence

`scripts/verify_backup_restore.sh`:

1. takes a content fingerprint (row count plus an ordered hash) of every table in
   `meta`, `raw`, `src`, `warehouse` and `marts`;
2. backs up;
3. damages the database (`DROP SCHEMA marts CASCADE; TRUNCATE raw.orders`) and checks that the
   damage took effect;
4. restores and compares the fingerprints.

Result on the Linux runner container (2026-09-29, `olist_dw_test` after the test suite):
`backup/restore round-trip OK: 41 tables identical in olist_dw_test`, in about 3 s.
The real `olist_dw` (734 MB) has not been timed.

## What is NOT covered

* **No point-in-time recovery.** There is no WAL archiving, so a restore returns to the last dump.
  Everything in `meta` written after that dump is lost; the warehouse itself can be rebuilt.
* **No schedule and no off-host copy.** `make backup` is manual, and `backups/` sits on the same
  disk as the volume. In production this would be a scheduled job that writes to object storage,
  with retention and a periodic restore test (the round-trip script is the restore test).
* Roles and grants are cluster-level objects created by `infra/postgres/init/01-bootstrap.sh`,
  not by the dump. To restore into a **new** instance, run `make up` first (bootstrap plus
  migrations), then `make restore`.
* Prometheus and Grafana volumes are not backed up (see the table for why).
