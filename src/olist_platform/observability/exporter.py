"""Prometheus exporter: metrics derived at scrape time from the meta.* tables (ADR-0007).

The pipeline is a batch job that exits, so it is not scraped directly and nothing is pushed.
The metadata tables are the system of record; they are append-only, so every counter below
is monotonic by construction and survives exporter restarts. Connects as the read-only
`olist_monitor` role (statement_timeout 10s).

Exposed metrics (Prometheus adds `_total` to counters, `_bucket/_sum/_count` to histograms):
  pipeline_runs_total{flow,status}                          counter
  pipeline_failures_total{flow,failed_task,error_type}      counter
  pipeline_duration_seconds{flow}                           histogram
  pipeline_last_run_duration_seconds{flow,status}           gauge
  pipeline_runs_in_progress{flow}                           gauge
  rows_processed_total{stage,source}                        counter
  rows_rejected_total{layer,source,rule,severity}           counter  (quarantined records)
  rows_flagged_total{layer,source,rule}                     counter  (WARNING records kept)
  data_quality_failures_total{rule,model,severity,blocking} counter  (failed rule evaluations)
  quality_gate_last_decision                                gauge    (1 PASS, 0 FAIL)
  dataset_last_published_timestamp_seconds                  gauge
  source_max_event_timestamp_seconds                        gauge
  olist_exporter_db_up                                      gauge    (1 if metadata readable)
dataset_freshness_seconds is a Prometheus recording rule: time() - last published timestamp.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence

from prometheus_client import CollectorRegistry, start_http_server
from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
    Metric,
)
from prometheus_client.registry import Collector
from sqlalchemy import Connection, Engine, text

from olist_platform.utils.logging import get_logger

DURATION_BUCKETS: tuple[float, ...] = (30, 60, 120, 300, 600, 1200, 1800, 3600, 7200)

log = get_logger(__name__)


def histogram_buckets(
    durations: Sequence[float], bounds: Sequence[float] = DURATION_BUCKETS
) -> list[tuple[str, float]]:
    """Cumulative Prometheus buckets (le -> count) for a list of observations."""
    buckets = [(str(float(b)), float(sum(1 for d in durations if d <= b))) for b in bounds]
    return [*buckets, ("+Inf", float(len(durations)))]


class MetadataCollector(Collector):
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def describe(self) -> list[Metric]:
        return []  # unchecked collector: families are produced at scrape time

    def collect(self) -> Iterator[Metric]:
        up = GaugeMetricFamily("olist_exporter_db_up", "1 if the metadata tables were readable")
        try:
            families = list(self._collect())
        except Exception as exc:
            log.error(
                "metrics_scrape_failed",
                error_type=type(exc).__name__,
                error=str(exc).splitlines()[0][:300],
            )
            up.add_metric([], 0)
            yield up
            return
        up.add_metric([], 1)
        yield up
        yield from families

    def _collect(self) -> Iterator[Metric]:
        with self.engine.connect() as conn:
            for group in (self._runs, self._volumes, self._quality, self._publication):
                yield from group(conn)

    def _runs(self, conn: Connection) -> Iterator[Metric]:
        """Run counts, failures, durations."""
        runs = CounterMetricFamily(
            "pipeline_runs", "Finished pipeline runs", labels=["flow", "status"]
        )
        for flow, status, n in conn.execute(
            text(
                "SELECT flow_name, status, count(*) FROM meta.pipeline_runs "
                "WHERE status <> 'running' GROUP BY 1, 2"
            )
        ):
            runs.add_metric([flow, status], n)
        yield runs

        in_progress = GaugeMetricFamily(
            "pipeline_runs_in_progress", "Runs not finished", labels=["flow"]
        )
        for flow, n in conn.execute(
            text(
                "SELECT flow_name, count(*) FROM meta.pipeline_runs "
                "WHERE status = 'running' GROUP BY 1"
            )
        ):
            in_progress.add_metric([flow], n)
        yield in_progress

        failures = CounterMetricFamily(
            "pipeline_failures",
            "Failed pipeline runs by failing task and error type",
            labels=["flow", "failed_task", "error_type"],
        )
        for flow, task, etype, n in conn.execute(
            text(
                "SELECT flow_name, coalesce(failed_task, 'unknown'), "
                "coalesce(error_type, 'unknown'), count(*) FROM meta.pipeline_runs "
                "WHERE status = 'failed' GROUP BY 1, 2, 3"
            )
        ):
            failures.add_metric([flow, task, etype], n)
        yield failures

        durations: dict[str, list[float]] = {}
        for flow, seconds in conn.execute(
            text(
                "SELECT flow_name, extract(epoch FROM finished_at - started_at)::float8 "
                "FROM meta.pipeline_runs WHERE finished_at IS NOT NULL"
            )
        ):
            durations.setdefault(flow, []).append(seconds)
        histogram = HistogramMetricFamily(
            "pipeline_duration_seconds", "Duration of finished pipeline runs", labels=["flow"]
        )
        for flow, values in durations.items():
            histogram.add_metric([flow], histogram_buckets(values), sum_value=sum(values))
        yield histogram

        last = GaugeMetricFamily(
            "pipeline_last_run_duration_seconds",
            "Duration of the most recent finished run",
            labels=["flow", "status"],
        )
        for flow, status, seconds in conn.execute(
            text(
                "SELECT DISTINCT ON (flow_name) flow_name, status, "
                "extract(epoch FROM finished_at - started_at)::float8 FROM meta.pipeline_runs "
                "WHERE finished_at IS NOT NULL ORDER BY flow_name, finished_at DESC"
            )
        ):
            last.add_metric([flow, status], seconds)
        yield last

    def _volumes(self, conn: Connection) -> Iterator[Metric]:
        """Rows processed, quarantined and flagged."""
        processed = CounterMetricFamily(
            "rows_processed",
            "Rows read by ingestion and written by staging",
            labels=["stage", "source"],
        )
        for stage, source, n in conn.execute(
            text(
                "SELECT task, source, sum(CASE WHEN task = 'ingest_raw' THEN rows_read "
                "ELSE rows_written END) FROM meta.ingestion_events "
                "WHERE status = 'ok' AND task IN ('ingest_raw', 'load_staging') "
                "GROUP BY 1, 2"
            )
        ):
            processed.add_metric([stage, source or "unknown"], float(n or 0))
        yield processed

        rejected = CounterMetricFamily(
            "rows_rejected",
            "Quarantined records (ERROR/CRITICAL, kept out of the data)",
            labels=["layer", "source", "rule", "severity"],
        )
        flagged = CounterMetricFamily(
            "rows_flagged",
            "Flagged records (WARNING, kept in the data)",
            labels=["layer", "source", "rule"],
        )
        for layer, source, rule, severity, action, n in conn.execute(
            text(
                "SELECT layer, source_table, rule_name, severity, action, count(*) "
                "FROM meta.rejected_records WHERE action IN ('quarantined', 'flagged') "
                "GROUP BY 1, 2, 3, 4, 5"
            )
        ):
            if action == "quarantined":
                rejected.add_metric([layer, source, rule, severity], n)
            else:
                flagged.add_metric([layer, source, rule], n)
        yield rejected
        yield flagged

    def _quality(self, conn: Connection) -> Iterator[Metric]:
        """Data-quality rule failures and the last gate decision."""
        dq = CounterMetricFamily(
            "data_quality_failures",
            "Failed data-quality rule evaluations",
            labels=["rule", "model", "severity", "blocking"],
        )
        for rule, model, severity, blocking, n in conn.execute(
            text(
                "SELECT test_name, coalesce(model, 'unknown'), severity, blocking, count(*) "
                "FROM meta.dq_results WHERE failures > 0 OR status IN ('error', 'skipped') "
                "GROUP BY 1, 2, 3, 4"
            )
        ):
            dq.add_metric([rule, model, severity, str(blocking).lower()], n)
        yield dq

        gate = GaugeMetricFamily(
            "quality_gate_last_decision", "Most recent quality gate decision (1 PASS, 0 FAIL)"
        )
        decision = conn.execute(
            text(
                "SELECT decision FROM meta.quality_gate_decisions ORDER BY decided_at DESC LIMIT 1"
            )
        ).scalar()
        if decision is not None:
            gate.add_metric([], 1 if decision == "PASS" else 0)
        yield gate

    def _publication(self, conn: Connection) -> Iterator[Metric]:
        """Publication time and business-data horizon."""
        published = GaugeMetricFamily(
            "dataset_last_published_timestamp_seconds",
            "Unix time of the last successful publication of warehouse/marts",
        )
        source_max = GaugeMetricFamily(
            "source_max_event_timestamp_seconds",
            "Latest business event (order purchase) in the published data - Olist ends in 2018",
        )
        row = conn.execute(
            text(
                "SELECT extract(epoch FROM published_at)::float8, "
                "extract(epoch FROM source_max_event_at)::float8 FROM meta.publications "
                "WHERE action = 'publish' ORDER BY published_at DESC LIMIT 1"
            )
        ).one_or_none()
        if row is not None:
            published.add_metric([], row[0])
            if row[1] is not None:
                source_max.add_metric([], row[1])
        yield published
        yield source_max


def serve(engine: Engine, port: int) -> None:
    registry = CollectorRegistry(auto_describe=False)
    registry.register(MetadataCollector(engine))
    start_http_server(port, registry=registry)
    log.info("metrics_exporter_started", port=port, status="ok")
    while True:  # the HTTP server runs in a daemon thread
        time.sleep(3600)
