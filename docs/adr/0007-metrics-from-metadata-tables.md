# ADR-0007: Prometheus metrics derived from metadata tables, not Pushgateway

**Status:** Accepted (2026-09-26)

## Context
The pipeline is a batch job: processes start, run for minutes and exit, so Prometheus cannot
scrape them directly. The usual workaround, Pushgateway, handles counters badly. Each push
*replaces* the previous value for its grouping key, so a `pipeline_runs_total` pushed by
separate processes never accumulates. A pushed value also stays in Pushgateway after the job is
gone.

## Decision
`meta.pipeline_runs`, `meta.ingestion_events`, `meta.rejected_records` and `meta.dq_results` are
the system of record. A small, long-running `metrics-exporter` service uses the read-only role
`olist_monitor`. It implements a custom `prometheus_client` collector that computes these
metrics at scrape time:

| Metric | Type | Derived from |
|--------|------|--------------|
| `pipeline_runs_total{status}` | counter | `count(*)` of runs by final status |
| `pipeline_failures_total{task,error_type}` | counter | failed runs |
| `pipeline_duration_seconds` | histogram | run durations, bucketed in SQL |
| `rows_processed_total{source}` / `rows_rejected_total{source,rule,severity}` | counter | ingestion events / quarantine |
| `data_quality_failures_total{rule,severity}` | counter | `dq_results` |
| `dataset_last_published_timestamp_seconds` | gauge | last successful publish |
| `dataset_freshness_seconds` | recording rule | `time() - dataset_last_published_timestamp_seconds` |

The metadata tables are append-only, so these counters can only go up.

A note on freshness: Olist's events end in 2018. Here, "freshness" means *time since the
warehouse was last successfully published*, not the age of the business events. The age of the
events is exposed separately as `source_max_event_timestamp_seconds`, so the freshness panel is
not misleading.

## Consequences
+ Metrics survive process exits and Prometheus restarts, and their history can be recomputed.
+ The README results, Grafana and SQL investigations all read from the same source of truth.
− Scrape cost grows with the volume of metadata. It is kept bounded by aggregating over indexes
  on `(status, finished_at)`, and is irrelevant at this scale.
− Logs are not shipped to Grafana (no Loki). JSON logs go to stdout and `logs/pipeline.jsonl`,
  and key events are also stored in `meta.ingestion_events`.
