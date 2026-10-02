# Documentation index

Files marked *generated* are written by a script and must not be edited by hand; the script
is named at the top of each file.

## Design

| Topic | Document |
|-------|----------|
| Architecture: data flow, control flow, observability and CI | [architecture.md](architecture.md) |
| Architecture decision records | [adr/](adr/README.md) |
| Dimensional model, grains, metric definitions | [data_model.md](data_model.md) |
| Model lineage (*generated*) | [lineage.md](lineage.md) |
| Database design: raw → src, constraints, index decisions | [database_design.md](database_design.md) |
| Data quality: severities, rule coverage, baselines | [data_quality.md](data_quality.md) |
| Source data contracts (*generated*) | [data_contracts.md](data_contracts.md) |
| Change detection (snapshot-level, not row-level incremental) | [incremental.md](incremental.md) |

## Operations

| Topic | Document |
|-------|----------|
| Runbook | [runbook.md](runbook.md) |
| Orchestration: flow, retry and timeout policy | [orchestration.md](orchestration.md) |
| Failure recovery: 11 scenarios | [failure-recovery.md](failure-recovery.md) |
| Backup and recovery | [backup-and-recovery.md](backup-and-recovery.md) |
| Observability: logs, metrics, alerts, dashboard | [observability.md](observability.md) |
| Security audit | [security.md](security.md) |

## Verification

| Topic | Document |
|-------|----------|
| Testing: behaviours → tests | [testing.md](testing.md) |
| Continuous integration and its run history | [ci.md](ci.md) |
| Production-readiness review: verified, not verified, risks | [production-readiness-review.md](production-readiness-review.md) |
| Benchmark (*generated*) | [benchmark.md](benchmark.md) |
| Query plans, EXPLAIN ANALYZE (*generated*) | [query_plans.md](query_plans.md) |
| Source data profile (*generated*) | [source_profile.md](source_profile.md) |
| Business metrics (*generated*) | [business_metrics.md](business_metrics.md) |
| Failures found during development and review | [what-failed.md](what-failed.md) |

## Author notes

Preparation material for presenting the project, not system documentation:
[interview-guide.md](interview-guide.md), [cv-project-description.md](cv-project-description.md).

## Repository layout

```
src/olist_platform/     ingestion, staging rule engine, publication, locking, quality gate, exporter, CLI (`olist`)
orchestration/          Prefect flow, retry and timeout policy, deployment server
dbt/                    models (staging → intermediate → core → marts), tests, macros
data_contracts/         one YAML contract per source file, and the committed checksum lock
migrations/             Alembic migrations for meta, raw and src
tests/                  unit/, integration/, e2e/, synthetic data builders
monitoring/             Prometheus configuration and rules; Grafana provisioning and dashboard
docker/, infra/         Dockerfiles; PostgreSQL bootstrap (roles, databases)
scripts/                benchmark, EXPLAIN analysis, business report, doc generators, backup/restore
benchmark/results.json  raw output of the last benchmark
```
