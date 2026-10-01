# Runbook

Day-to-day operations. `make help` lists every command. Each make target is a thin wrapper
around `docker compose`, so the raw command is shown where it helps.

| Task | Command |
|------|---------|
| Start everything and wait until healthy | `make up` |
| Run a refresh now (CLI, one run) | `make pipeline` |
| Run a refresh through the deployment | `docker compose exec pipeline-worker prefect deployment run olist_refresh/olist-refresh` |
| Force a rebuild although inputs are unchanged | `make pipeline-full` (`olist run --full-refresh`) |
| Why was a run skipped? | `SELECT event, details FROM meta.ingestion_events WHERE task = 'detect_changes' ORDER BY created_at DESC LIMIT 5;` (`reason`: inputs_unchanged, inputs_changed, never_published, ...) |
| Why did a run fail? | `SELECT failed_task, error_type, error_message FROM meta.pipeline_runs ORDER BY started_at DESC LIMIT 5;` then Grafana "Failures by task" |
| A run shows `failed / abandoned` | the process was killed (no failure handler ran); nothing was published; the next run starts from empty build schemas |
| What was quarantined? | `SELECT layer, source_table, rule_name, severity, reason, raw_record FROM meta.rejected_records WHERE pipeline_run_id = '<id>';` |
| Why did the gate fail? | `SELECT reasons FROM meta.quality_gate_decisions WHERE pipeline_run_id = '<id>';` and the failing rows in `dq_failures.<test>` |
| Undo a bad publication | `docker compose run --rm dev olist rollback-publish` (the next run rebuilds the current inputs) |
| Reload a file | `docker compose run --rm dev olist ingest --force-reload` |
| Verify that two environments match | `docker compose run --rm dev olist fingerprint` on both, then diff |
| Back up / restore the warehouse database | `make backup` / `make restore FILE=backups/<file>.dump` ([backup-and-recovery.md](backup-and-recovery.md)) |
| Prove a restore works | `make verify-backup DB=olist_dw_test` |
| Downgrade migrations below 0003 | `olist db drop-derived --yes`, then `alembic downgrade <rev>` |
| Stop (keep data) / stop and delete all data | `make down` / `make reset` |

Failure scenarios and the platform's response: [failure-recovery.md](failure-recovery.md).
