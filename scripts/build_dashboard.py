"""Generate monitoring/grafana/dashboards/olist_pipeline.json (provisioned into Grafana).

Generated rather than hand-edited so the dashboard is reviewable as code and a test can
check that every panel query only uses metrics the exporter actually exposes.

Design rules (dataviz method): headline numbers are stat tiles, not charts; one axis per
panel; status colours (good/warning/critical) are reserved for states and always paired with
a text value; every multi-series panel has a legend.

Usage: python scripts/build_dashboard.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUT = (
    Path(__file__).resolve().parents[1]
    / "monitoring"
    / "grafana"
    / "dashboards"
    / "olist_pipeline.json"
)
DS = {"type": "prometheus", "uid": "prometheus"}
REFRESH_BUCKETS = 'sum by (le) (pipeline_duration_seconds_bucket{flow="olist_refresh"})'

# Reserved status palette (never used for series identity)
GOOD, WARNING, SERIOUS, CRITICAL = "#0ca30c", "#fab219", "#ec835a", "#d03b3b"
SEVERITY_COLOR = {"WARNING": WARNING, "ERROR": SERIOUS, "CRITICAL": CRITICAL}

_next_id = iter(range(1, 1000))


def target(expr: str, legend: str = "", instant: bool = False, ref: str = "A") -> dict[str, Any]:
    return {
        "datasource": DS,
        "expr": expr,
        "legendFormat": legend,
        "refId": ref,
        "instant": instant,
        "range": not instant,
    }


def panel(
    kind: str,
    title: str,
    desc: str,
    pos: tuple[int, int, int, int],
    targets: list[dict],
    field: dict | None = None,
    options: dict | None = None,
    overrides: list | None = None,
    transformations: list | None = None,
) -> dict[str, Any]:
    x, y, w, h = pos
    return {
        "id": next(_next_id),
        "type": kind,
        "title": title,
        "description": desc,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "datasource": DS,
        "targets": targets,
        "fieldConfig": {"defaults": field or {}, "overrides": overrides or []},
        "options": options or {},
        "transformations": transformations or [],
    }


def table_target(expr: str, ref: str = "A") -> dict[str, Any]:
    """Instant query in table format: every label becomes a column."""
    return {**target(expr, instant=True, ref=ref), "format": "table"}


def tidy_table(
    rename: dict[str, str], merge: bool = False, order: list[str] | None = None
) -> list[dict[str, Any]]:
    """Grafana shows ONE frame per table unless frames are merged: a multi-query table
    without `merge` silently hides every query but the first (found on the live dashboard)."""
    steps: list[dict[str, Any]] = [{"id": "merge", "options": {}}] if merge else []
    options: dict[str, Any] = {"excludeByName": {"Time": True}, "renameByName": rename}
    if order:
        options["indexByName"] = {name: i for i, name in enumerate(order)}
    return [*steps, {"id": "organize", "options": options}]


def stat_options(text_mode: str = "value_and_name") -> dict[str, Any]:
    return {
        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        "colorMode": "background",
        "graphMode": "none",
        "textMode": text_mode,
        "justifyMode": "center",
    }


def thresholds(*steps: tuple[float | None, str]) -> dict[str, Any]:
    return {"mode": "absolute", "steps": [{"value": v, "color": c} for v, c in steps]}


def severity_overrides() -> list[dict[str, Any]]:
    return [
        {
            "matcher": {"id": "byName", "options": sev},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}],
        }
        for sev, color in SEVERITY_COLOR.items()
    ]


def build() -> dict[str, Any]:
    p: list[dict[str, Any]] = []
    # ---- row 1: headline stat tiles -----------------------------------------------------
    p.append(
        panel(
            "stat",
            "Pipeline success rate",
            "Share of finished olist_refresh runs that succeeded "
            "(all time, from meta.pipeline_runs).",
            (0, 0, 4, 5),
            [
                target(
                    'sum(pipeline_runs_total{flow="olist_refresh",status="success"}) / '
                    'sum(pipeline_runs_total{flow="olist_refresh"})',
                    instant=True,
                )
            ],
            {
                "unit": "percentunit",
                "decimals": 1,
                "thresholds": thresholds((None, CRITICAL), (0.9, WARNING), (0.99, GOOD)),
            },
            stat_options("value"),
        )
    )
    p.append(
        panel(
            "stat",
            "Last quality gate",
            "Most recent quality-gate decision. FAIL = publication "
            "blocked; consumers still see the previous version.",
            (4, 0, 4, 5),
            [target("quality_gate_last_decision", instant=True)],
            {
                "mappings": [
                    {
                        "type": "value",
                        "options": {
                            "1": {"text": "PASS", "color": GOOD, "index": 0},
                            "0": {"text": "FAIL", "color": CRITICAL, "index": 1},
                        },
                    }
                ],
                "thresholds": thresholds((None, CRITICAL), (1, GOOD)),
            },
            stat_options("value"),
        )
    )
    p.append(
        panel(
            "stat",
            "Warehouse freshness",
            "Time since the last successful publication "
            "(recording rule dataset_freshness_seconds). Olist business events end in 2018: see "
            "'Source data horizon'.",
            (8, 0, 4, 5),
            [target("dataset_freshness_seconds", instant=True)],
            {
                "unit": "s",
                "decimals": 0,
                "thresholds": thresholds((None, GOOD), (26 * 3600, WARNING), (48 * 3600, CRITICAL)),
            },
            stat_options("value"),
        )
    )
    p.append(
        panel(
            "stat",
            "Last refresh duration",
            "Wall-clock duration of the most recent finished olist_refresh run.",
            (12, 0, 4, 5),
            [target('max(pipeline_last_run_duration_seconds{flow="olist_refresh"})', instant=True)],
            {"unit": "s", "decimals": 0, "color": {"mode": "fixed", "fixedColor": "text"}},
            {**stat_options("value"), "colorMode": "none"},
        )
    )
    p.append(
        panel(
            "stat",
            "Source data horizon",
            "Latest order purchase in the published warehouse. Fixed for this historical snapshot.",
            (16, 0, 4, 5),
            [target("source_max_event_timestamp_seconds * 1000", instant=True)],
            {
                "unit": "dateTimeAsIsoNoDateIfToday",
                "color": {"mode": "fixed", "fixedColor": "text"},
            },
            {**stat_options("value"), "colorMode": "none"},
        )
    )
    p.append(
        panel(
            "stat",
            "Metrics source",
            "1 = the exporter can read the metadata tables.",
            (20, 0, 4, 5),
            [target('min(olist_exporter_db_up) and min(up{job="olist-metrics"})', instant=True)],
            {
                "mappings": [
                    {
                        "type": "value",
                        "options": {
                            "1": {"text": "UP", "color": GOOD, "index": 0},
                            "0": {"text": "DOWN", "color": CRITICAL, "index": 1},
                        },
                    }
                ],
                "thresholds": thresholds((None, CRITICAL), (1, GOOD)),
            },
            stat_options("value"),
        )
    )

    # ---- row 2: runs and runtime --------------------------------------------------------
    p.append(
        panel(
            "timeseries",
            "Finished runs by flow and status (cumulative)",
            "Counters derived from meta.pipeline_runs; each step up is one finished run.",
            (0, 5, 12, 8),
            [target("sum by (flow, status) (pipeline_runs_total)", "{{flow}} · {{status}}")],
            {
                "custom": {
                    "lineWidth": 2,
                    "drawStyle": "line",
                    "lineInterpolation": "stepAfter",
                    "fillOpacity": 0,
                    "showPoints": "never",
                },
                "decimals": 0,
            },
            {
                "legend": {"displayMode": "table", "placement": "right", "calcs": ["lastNotNull"]},
                "tooltip": {"mode": "multi", "sort": "desc"},
            },
        )
    )
    p.append(
        panel(
            "bargauge",
            "Run duration percentiles",
            "Estimated from the pipeline_duration_seconds histogram over all finished runs.",
            (12, 5, 6, 8),
            [
                target(
                    f"histogram_quantile(0.5, {REFRESH_BUCKETS})",
                    "p50",
                    instant=True,
                    ref="A",
                ),
                target(
                    f"histogram_quantile(0.95, {REFRESH_BUCKETS})",
                    "p95",
                    instant=True,
                    ref="B",
                ),
            ],
            {"unit": "s", "decimals": 0, "color": {"mode": "fixed", "fixedColor": "#2a78d6"}},
            {
                "orientation": "horizontal",
                "displayMode": "basic",
                "showUnfilled": True,
                "reduceOptions": {"calcs": ["lastNotNull"], "values": False},
            },
        )
    )
    p.append(
        panel(
            "table",
            "Failures by task and error type",
            "Which step failed and why "
            "(pipeline_failures_total). Deterministic errors are never retried.",
            (18, 5, 6, 8),
            [table_target("sum by (flow, failed_task, error_type) (pipeline_failures_total)")],
            {},
            {"showHeader": True, "cellHeight": "sm"},
            transformations=tidy_table({"Value": "Failed runs"}),
        )
    )

    # ---- row 3: data volume and quality -------------------------------------------------
    p.append(
        panel(
            "bargauge",
            "Rows processed by source",
            "Rows read by ingest_raw (files) and written "
            "by load_staging (typed src tables), all runs.",
            (0, 13, 8, 10),
            [
                target(
                    'sum by (source) (rows_processed_total{stage="load_staging"})',
                    "{{source}}",
                    instant=True,
                )
            ],
            {"decimals": 0, "color": {"mode": "fixed", "fixedColor": "#2a78d6"}},
            {
                "orientation": "horizontal",
                "displayMode": "basic",
                "showUnfilled": False,
                "reduceOptions": {"calcs": ["lastNotNull"], "values": False},
            },
        )
    )
    p.append(
        panel(
            "table",
            "Quarantined and flagged records by rule",
            "Quarantined = kept out of the "
            "data (ERROR). Flagged = kept, WARNING. Every record is stored in "
            "meta.rejected_records with rule, severity, run and reason.",
            (8, 13, 8, 10),
            [
                table_target("sum by (layer, source, rule) (rows_rejected_total)", ref="A"),
                table_target("sum by (layer, source, rule) (rows_flagged_total)", ref="B"),
            ],
            {},
            {
                "showHeader": True,
                "cellHeight": "sm",
                "sortBy": [{"displayName": "Flagged (WARNING, kept)", "desc": True}],
            },
            transformations=tidy_table(
                {"Value #A": "Quarantined (ERROR)", "Value #B": "Flagged (WARNING, kept)"},
                merge=True,
                order=["layer", "source", "rule", "Value #A", "Value #B"],
            ),
        )
    )
    p.append(
        panel(
            "bargauge",
            "Data-quality failures by severity",
            "Failed rule evaluations "
            "(data_quality_failures_total). Only CRITICAL, or ERROR beyond its tolerance, blocks "
            "publication.",
            (16, 13, 8, 10),
            [
                target(
                    "sum by (severity) (data_quality_failures_total)", "{{severity}}", instant=True
                )
            ],
            {"decimals": 0, "color": {"mode": "fixed", "fixedColor": "text"}},
            {
                "orientation": "horizontal",
                "displayMode": "basic",
                "showUnfilled": False,
                "reduceOptions": {"calcs": ["lastNotNull"], "values": False},
            },
            severity_overrides(),
        )
    )
    return {
        "uid": "olist-pipeline",
        "title": "Olist pipeline",
        "tags": ["olist", "data-platform"],
        "timezone": "utc",
        "schemaVersion": 39,
        "version": 1,
        "editable": False,
        "time": {"from": "now-24h", "to": "now"},
        "refresh": "30s",
        "panels": p,
        "templating": {"list": []},
        "annotations": {"list": []},
    }


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
