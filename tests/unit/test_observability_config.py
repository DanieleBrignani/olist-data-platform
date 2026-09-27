"""Monitoring-as-code checks: the dashboard and alert rules can only reference metrics the
exporter actually produces (a renamed metric would otherwise leave a silently empty panel)."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import yaml

from olist_platform.observability.exporter import histogram_buckets

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / "monitoring" / "grafana" / "dashboards" / "olist_pipeline.json"
RULES = ROOT / "monitoring" / "prometheus" / "rules.yml"

# Families produced by MetadataCollector, with the suffixes Prometheus adds
EXPOSED = {
    "pipeline_runs_total",
    "pipeline_failures_total",
    "pipeline_duration_seconds_bucket",
    "pipeline_duration_seconds_sum",
    "pipeline_duration_seconds_count",
    "pipeline_last_run_duration_seconds",
    "pipeline_runs_in_progress",
    "rows_processed_total",
    "rows_rejected_total",
    "rows_flagged_total",
    "data_quality_failures_total",
    "quality_gate_last_decision",
    "dataset_last_published_timestamp_seconds",
    "source_max_event_timestamp_seconds",
    "olist_exporter_db_up",
    "dataset_freshness_seconds",  # recording rule
}
OUR_METRIC = re.compile(
    r"\b((?:pipeline|rows|data_quality|quality_gate|dataset|source_max|olist)_[a-z_]+)\b"
)


def referenced_metrics(expr: str) -> set[str]:
    # label VALUES (quoted) are not metric names: drop them before matching
    return set(OUR_METRIC.findall(re.sub(r'"[^"]*"', '""', expr)))


def dashboard_exprs() -> list[str]:
    doc = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    return [t["expr"] for p in doc["panels"] for t in p["targets"]]


def rule_exprs() -> list[str]:
    doc = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    return [r["expr"] for g in doc["groups"] for r in g["rules"]]


def test_histogram_buckets_are_cumulative_with_inf() -> None:
    assert histogram_buckets([10, 45, 100, 9000], bounds=(30, 60, 120)) == [
        ("30.0", 1.0),
        ("60.0", 2.0),
        ("120.0", 3.0),
        ("+Inf", 4.0),
    ]


def test_freshness_is_a_recording_rule_over_the_publication_gauge() -> None:
    doc = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    records = {r["record"]: r["expr"] for g in doc["groups"] for r in g["rules"] if "record" in r}
    assert records["dataset_freshness_seconds"] == (
        "time() - dataset_last_published_timestamp_seconds"
    )


def test_dashboard_and_rules_only_use_exposed_metrics() -> None:
    for expr in dashboard_exprs() + rule_exprs():
        unknown = referenced_metrics(expr) - EXPOSED
        assert not unknown, f"{expr!r} references unknown metrics {unknown}"


def test_dashboard_covers_the_required_views() -> None:
    doc = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    titles = " | ".join(p["title"].lower() for p in doc["panels"])
    for required in (
        "success rate",
        "duration",
        "rows processed",
        "quarantined",
        "data-quality failures",
        "freshness",
    ):
        assert required in titles, required
    used = set().union(*(referenced_metrics(e) for e in dashboard_exprs()))
    assert {
        "pipeline_runs_total",
        "rows_processed_total",
        "rows_rejected_total",
        "data_quality_failures_total",
        "dataset_freshness_seconds",
    } <= used


def test_committed_dashboard_matches_generator() -> None:
    spec = importlib.util.spec_from_file_location(
        "build_dashboard", ROOT / "scripts" / "build_dashboard.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    committed = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    assert committed == module.build(), "run `python scripts/build_dashboard.py`"


def test_multi_query_tables_merge_their_frames() -> None:
    """Regression: a table with several queries shows only the first frame unless merged
    (the live dashboard hid all flagged records behind an empty quarantine query)."""
    doc = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    for p in doc["panels"]:
        if p["type"] == "table" and len(p["targets"]) > 1:
            ids = [t["id"] for t in p.get("transformations", [])]
            assert "merge" in ids, p["title"]
