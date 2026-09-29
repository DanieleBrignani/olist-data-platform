"""Reproducible benchmark on the REAL Olist v2 dataset. Every number it prints is measured.

Measures, for each repetition (empty database -> initial load -> rerun -> forced rebuild):
  * initial load runtime per step and end to end, rows/second for ingestion and staging,
    dbt build + test time;
  * rerun runtime with unchanged inputs (checksum skip, change detection -> no rebuild);
  * forced rebuild of the same inputs (`--full-refresh`: staging, dbt, gate, publish), i.e.
    what every rerun cost before change detection;
  * database size by schema after the load;
and once afterwards:
  * representative reporting-query latency (median / p95 over N warm runs, reporting role);
  * a content fingerprint of every published table per repetition (determinism check).

Writes docs/benchmark.md, benchmark/results.json and (with --update-readme) the marked
"Measured Results" block of README.md.

DESTRUCTIVE: it empties meta/raw/src and drops the dbt schemas of the target database, so it
refuses to run without --reset. Run it in an isolated compose project (fresh volume):

    docker compose -p olistbench up -d --wait postgres
    docker compose -p olistbench up --exit-code-from migrate migrate
    docker compose -p olistbench run --rm dev python scripts/benchmark.py --reset --update-readme
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text

from olist_platform.config import Role, get_settings
from olist_platform.database.change_detection import evaluate
from olist_platform.database.engine import get_engine
from olist_platform.database.fingerprint import fingerprint
from olist_platform.database.publish import publish
from olist_platform.database.staging import load_staging
from olist_platform.ingestion.manifest import LOCK_PATH, load_lock
from olist_platform.ingestion.pipeline import (
    ingest_raw,
    validate_source,
    verify_manifest,
)
from olist_platform.ingestion.runs import tracked_run
from olist_platform.ingestion.source import raw_dir
from olist_platform.transform.warehouse import dbt_build, dbt_test, quality_gate
from olist_platform.utils.logging import configure_logging
from olist_platform.validation.contracts import load_contracts

ROOT = Path(__file__).resolve().parents[1]
DERIVED = (
    "stg",
    "int",
    "warehouse_build",
    "marts_build",
    "warehouse",
    "marts",
    "warehouse_prev",
    "marts_prev",
    "dq_failures",
)
README_START, README_END = "<!-- BENCHMARK:START -->", "<!-- BENCHMARK:END -->"

QUERIES: dict[str, tuple[str, str]] = {
    "Q1 monthly GMV & AOV (mart)": (
        "Dashboard query served by the pre-aggregated mart",
        "SELECT year_month, orders, gmv, avg_order_value FROM marts.mart_sales ORDER BY 1",
    ),
    "Q2 monthly GMV & AOV (from facts)": (
        "Same answer computed from the order fact: the cost the mart avoids",
        "SELECT d.year_month, count(*), sum(f.order_value) FILTER (WHERE NOT f.is_canceled) "
        "FROM warehouse.fct_orders f JOIN warehouse.dim_date d ON d.date_key = f.purchase_date_key "
        "GROUP BY 1 ORDER BY 1",
    ),
    "Q3 late-delivery rate by state, 2018": (
        "Delivery performance slice",
        "SELECT customer_state, sum(delivered_orders), sum(late_orders), "
        "round(sum(late_orders)::numeric / sum(delivered_orders), 4) "
        "FROM marts.mart_delivery_performance WHERE month_start >= '2018-01-01' "
        "GROUP BY 1 ORDER BY 4 DESC",
    ),
    "Q4 top-10 categories by revenue": (
        "Category performance",
        "SELECT category_name, revenue, avg_review_score, late_rate "
        "FROM marts.mart_category_performance ORDER BY revenue DESC LIMIT 10",
    ),
    "Q5 one seller's monthly revenue (facts)": (
        "Operational drill-down on the item fact (indexed seller_id)",
        "SELECT d.year_month, sum(i.price) FROM warehouse.fct_order_items i "
        "JOIN warehouse.dim_date d ON d.date_key = i.purchase_date_key "
        "WHERE i.seller_id = :seller GROUP BY 1 ORDER BY 1",
    ),
    "Q6 one customer's order history (facts)": (
        "Customer-service lookup (indexed customer_unique_id)",
        "SELECT order_id, purchased_at, order_status, order_value, is_late "
        "FROM warehouse.fct_orders WHERE customer_unique_id = :customer ORDER BY purchased_at",
    ),
}


class Timer:
    def __init__(self) -> None:
        self.steps: dict[str, float] = {}

    @contextmanager
    def step(self, name: str):
        started = time.perf_counter()
        yield
        self.steps[name] = round(time.perf_counter() - started, 3)


def reset(pipeline_engine: Any, admin_engine: Any) -> None:
    with pipeline_engine.begin() as conn:
        for schema in DERIVED:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
    with admin_engine.begin() as conn:
        tables = (
            conn.execute(
                text(
                    "SELECT schemaname || '.' || tablename FROM pg_tables "
                    "WHERE schemaname IN ('meta', 'raw', 'src')"
                )
            )
            .scalars()
            .all()
        )
        conn.execute(text(f"TRUNCATE {', '.join(tables)} CASCADE"))
    with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("VACUUM"))  # comparable starting point for every repetition


def run_cycle(
    label: str, force_reload: bool, timer: Timer, dbt_target: str, full_refresh: bool = False
) -> dict[str, Any]:
    engine = get_engine(Role.PIPELINE)
    lock, contracts = load_lock(LOCK_PATH), load_contracts()
    directory = raw_dir(ROOT / "data")
    out: dict[str, Any] = {"label": label}
    started = time.perf_counter()
    with tracked_run(engine, f"benchmark_{label}", get_settings().env, lock.dataset_version) as run:
        with timer.step("verify_manifest"):
            checksums = verify_manifest(directory, lock)
        with timer.step("validate_source"):
            validate_source(directory, contracts)
        with timer.step("ingest_raw"):
            files = ingest_raw(
                engine, run.pipeline_run_id, directory, lock, checksums, contracts, force_reload
            )
        with timer.step("detect_changes"), engine.connect() as conn:
            changes = evaluate(conn, full_refresh or force_reload)
        out["rebuild"] = changes.rebuild
        if changes.rebuild:
            with timer.step("load_staging"):
                tables = load_staging(engine, run.pipeline_run_id, contracts)
            with timer.step("dbt_build"):
                dbt_build(dbt_target)
            with timer.step("dbt_test"):
                tests = dbt_test(dbt_target)
            with timer.step("quality_gate"):
                decision = quality_gate(engine, run.pipeline_run_id, tests)
            with timer.step("publish_marts"):
                publish(engine, run.pipeline_run_id, changes.fingerprint)
    out["total_s"] = round(time.perf_counter() - started, 3)
    out["steps_s"] = dict(timer.steps)
    out["rows_ingested"] = sum(f.rows_loaded for f in files)
    out["files_loaded"] = sum(f.status == "loaded" for f in files)
    out["files_skipped"] = sum(f.status == "skipped" for f in files)
    if not changes.rebuild:
        out["gate"] = f"skipped ({changes.reason})"
        return out
    out["rows_staged_in"] = sum(t.raw_rows for t in tables)
    out["rows_staged_out"] = sum(t.src_rows for t in tables)
    out["quarantined"] = sum(t.quarantined for t in tables)
    out["flagged"] = sum(t.flagged for t in tables)
    out["dq_tests"] = decision.evaluated
    out["dq_warnings"] = len(decision.warnings)
    out["gate"] = "PASS" if decision.passed else "FAIL"
    return out


def database_size() -> dict[str, int]:
    with get_engine(Role.ADMIN).connect() as conn:
        total = conn.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
        schemas = dict(
            conn.execute(
                text(
                    "SELECT n.nspname, coalesce(sum(pg_total_relation_size(c.oid)), 0)::bigint "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relkind IN ('r', 'm') AND n.nspname IN "
                    "('meta','raw','src','warehouse','marts','dq_failures') GROUP BY 1"
                )
            ).all()
        )
    return {"database": total, **schemas}


def query_latency(runs: int) -> dict[str, dict[str, float]]:
    engine = get_engine(Role.REPORTING)
    with engine.connect() as conn:
        params = {
            "seller": conn.execute(
                text(
                    "SELECT seller_id FROM warehouse.fct_order_items GROUP BY 1 "
                    "ORDER BY count(*) DESC, 1 LIMIT 1 OFFSET 49"
                )
            ).scalar_one(),
            "customer": conn.execute(
                text(
                    "SELECT customer_unique_id FROM warehouse.fct_orders GROUP BY 1 "
                    "ORDER BY count(*) DESC, 1 LIMIT 1"
                )
            ).scalar_one(),
        }
        results = {}
        for name, (_, sql) in QUERIES.items():
            conn.execute(text(sql), params).all()  # warm-up
            samples = []
            for _ in range(runs):
                started = time.perf_counter()
                conn.execute(text(sql), params).all()
                samples.append((time.perf_counter() - started) * 1000)
            samples.sort()
            results[name] = {
                "median_ms": round(statistics.median(samples), 2),
                "p95_ms": round(
                    samples[max(0, math.ceil(0.95 * len(samples)) - 1)], 2
                ),  # nearest rank
                "runs": runs,
            }
    return results


def environment() -> dict[str, Any]:
    with get_engine(Role.ADMIN).connect() as conn:
        pg = conn.execute(text("SHOW server_version")).scalar_one()
        settings = dict(
            conn.execute(
                text(
                    "SELECT name, setting || coalesce(unit, '') FROM pg_settings WHERE name IN "
                    "('shared_buffers', 'work_mem', 'max_parallel_workers_per_gather')"
                )
            ).all()
        )
    mem_kb = (
        next(
            (
                int(line.split()[1])
                for line in Path("/proc/meminfo").read_text().splitlines()
                if line.startswith("MemTotal")
            ),
            0,
        )
        if Path("/proc/meminfo").exists()
        else 0
    )
    # the container image has no git: the host passes the commit in
    commit = os.environ.get("BENCHMARK_GIT_COMMIT", "n/a")
    return {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_commit": commit,
        "python": platform.python_version(),
        "postgres": pg,
        "postgres_settings": settings,
        "cpus_visible": os.cpu_count(),
        "memory_visible_gb": round(mem_kb / 1024 / 1024, 1),
        "dataset": f"{load_lock().dataset} v{load_lock().dataset_version}",
        "host_note": os.environ.get("BENCHMARK_HOST_NOTE", ""),
    }


def summarise(values: list[float]) -> dict[str, float]:
    return {
        "median": round(statistics.median(values), 2),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
    }


def mb(value: int) -> str:
    return f"{value / 1024 / 1024:,.1f} MB"


def render(results: dict[str, Any]) -> tuple[str, str]:
    reps = results["repetitions"]
    initial = [r["initial"] for r in reps]
    rerun = [r["rerun"] for r in reps]
    forced = [r["forced"] for r in reps]
    agg = results["aggregate"]
    env = results["environment"]
    size = results["database_size"]
    lat = results["query_latency"]
    n = len(reps)

    def s(key: str) -> str:
        v = agg[key]
        return f"{v['median']:,.1f} s (min {v['min']:,.1f} / max {v['max']:,.1f})"

    readme = "\n".join(
        [
            f"Measured by `scripts/benchmark.py` on {env['measured_at'][:10]} (commit "
            f"`{env['git_commit']}`), real Olist v2 data, median of {n} repetitions from an empty "
            f"database. Environment and method: [docs/benchmark.md](docs/benchmark.md).",
            "",
            "| Measure | Result |",
            "|---------|--------|",
            f"| Source records ingested (9 files) | {initial[0]['rows_ingested']:,} |",
            f"| Records quarantined / flagged by contracts | {initial[0]['quarantined']:,} / "
            f"{initial[0]['flagged']:,} |",
            f"| Data-quality tests evaluated / warnings / gate | {initial[0]['dq_tests']} / "
            f"{initial[0]['dq_warnings']} / {initial[0]['gate']} |",
            f"| Initial load, end to end (empty DB → published marts) | {s('initial_total_s')} |",
            f"| Rerun with unchanged inputs (checksum skip, change detection: no rebuild) | "
            f"{s('rerun_total_s')} |",
            f"| Forced rebuild of unchanged inputs (`--full-refresh`: staging, dbt, gate, "
            f"publish) | {s('forced_total_s')} |",
            f"| Ingestion throughput | {agg['ingest_rows_per_s']['median']:,.0f} rows/s |",
            f"| Staging throughput (raw → typed, all rules) | "
            f"{agg['staging_rows_per_s']['median']:,.0f} rows/s |",
            f"| dbt build (33 models) + dbt test ({initial[0]['dq_tests']} tests) | "
            f"{s('dbt_build_s')} + {s('dbt_test_s')} |",
            f"| Database size after load | {mb(size['database'])} |",
            "| Reporting query latency, median (p95) | "
            + "; ".join(
                f"{k.split(' ', 1)[0]} {v['median_ms']:,.1f} ms ({v['p95_ms']:,.1f})"
                for k, v in lat.items()
            )
            + " |",
            f"| Published tables identical across all {n} repetitions and forced rebuilds "
            f"(content md5) | "
            f"{'yes' if results['deterministic'] else 'NO'} |",
        ]
    )

    lines = [
        "# Benchmark",
        "",
        f"Generated by `scripts/benchmark.py` on {env['measured_at']}. Do not edit by hand.",
        "",
        "## Environment",
        "",
        "| Item | Value |",
        "|------|-------|",
        *[f"| {k} | {v} |" for k, v in env.items() if v not in ("", None)],
        "",
        "Timings depend on this environment and are comparable only with each other. "
        "Re-run the benchmark on your own machine to get your numbers.",
        "",
        "## Method",
        "",
        f"* {n} repetitions. Each starts from an **empty** database (meta/raw/src truncated, "
        "dbt schemas dropped, `VACUUM`), then runs the initial load, a rerun with the same "
        "files (expected: skipped by change detection), and a forced rebuild of the same "
        "inputs (`full_refresh`).",
        "* Steps are the same functions the Prefect flow calls, timed without orchestrator "
        "overhead.",
        "* Query latency: client-side wall time as `olist_reporting` on the published schemas, "
        f"1 warm-up + {next(iter(lat.values()))['runs']} timed runs each.",
        "* Determinism: every repetition fingerprints all published tables (row count + md5).",
        "",
        "## Results",
        "",
        readme.split("\n", 2)[2],
        "",
        "## Initial load by step (seconds, median / min / max)",
        "",
        "| Step | Median | Min | Max |",
        "|------|-------:|----:|----:|",
        *[
            f"| {step} | {v['median']:.2f} | {v['min']:.2f} | {v['max']:.2f} |"
            for step, v in agg["initial_steps"].items()
        ],
        "",
        "## Rerun by step (seconds, median / min / max)",
        "",
        "| Step | Median | Min | Max |",
        "|------|-------:|----:|----:|",
        *[
            f"| {step} | {v['median']:.2f} | {v['min']:.2f} | {v['max']:.2f} |"
            for step, v in agg["rerun_steps"].items()
        ],
        "",
        "## Forced rebuild by step (seconds, median / min / max)",
        "",
        "| Step | Median | Min | Max |",
        "|------|-------:|----:|----:|",
        *[
            f"| {step} | {v['median']:.2f} | {v['min']:.2f} | {v['max']:.2f} |"
            for step, v in agg["forced_steps"].items()
        ],
        "",
        "## Database size after load",
        "",
        "| Scope | Size |",
        "|-------|-----:|",
        *[f"| {k} | {mb(v)} |" for k, v in sorted(size.items(), key=lambda kv: -kv[1])],
        "",
        "## Query latency",
        "",
        "| Query | What it represents | Median (ms) | p95 (ms) |",
        "|-------|--------------------|------------:|---------:|",
        *[
            f"| {k} | {QUERIES[k][0]} | {v['median_ms']:,.2f} | {v['p95_ms']:,.2f} |"
            for k, v in lat.items()
        ],
        "",
        "## Per repetition",
        "",
        "| # | Initial (s) | Rerun (s) | Forced rebuild (s) | Files loaded / skipped on rerun "
        "| Gate: initial / rerun / forced |",
        "|---|---:|---:|---:|---|---|",
        *[
            f"| {i + 1} | {a['total_s']:.1f} | {b['total_s']:.1f} | {c['total_s']:.1f} | "
            f"{a['files_loaded']} / {b['files_skipped']} | {a['gate']} / {b['gate']} / "
            f"{c['gate']} |"
            for i, (a, b, c) in enumerate(zip(initial, rerun, forced, strict=True))
        ],
    ]
    return "\n".join(lines) + "\n", readme


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reset",
        action="store_true",
        help="required: empties the target database before each repetition",
    )
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--query-runs", type=int, default=20)
    parser.add_argument("--update-readme", action="store_true")
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("--repetitions must be >= 1")
    if not args.reset:
        print(
            "refusing to run without --reset (it empties the target database); "
            "use an isolated compose project, see the module docstring",
            file=sys.stderr,
        )
        return 2

    settings = get_settings()
    configure_logging("WARNING", settings.env)
    dbt_target = "/tmp/benchmark-dbt"  # noqa: S108 - container-local scratch
    reps: list[dict[str, Any]] = []
    fingerprints: list[dict[str, str]] = []
    for i in range(args.repetitions):
        print(f"repetition {i + 1}/{args.repetitions}: reset", flush=True)
        reset(get_engine(Role.PIPELINE), get_engine(Role.ADMIN))
        initial = run_cycle("initial", False, Timer(), dbt_target)
        print(f"  initial load {initial['total_s']:.1f}s", flush=True)
        if i == 0:
            size = database_size()
        with get_engine(Role.REPORTING).connect() as conn:
            after_initial = {t: f.md5 for t, f in fingerprint(conn).items()}
        rerun = run_cycle("rerun", False, Timer(), dbt_target)
        print(f"  rerun {rerun['total_s']:.1f}s (rebuild: {rerun['rebuild']})", flush=True)
        forced = run_cycle("forced_rebuild", False, Timer(), dbt_target, full_refresh=True)
        print(f"  forced rebuild {forced['total_s']:.1f}s", flush=True)
        with get_engine(Role.REPORTING).connect() as conn:
            fingerprints.append({t: f.md5 for t, f in fingerprint(conn).items()})
        # a rebuild from identical inputs must publish identical tables
        fingerprints.append(after_initial)
        reps.append({"initial": initial, "rerun": rerun, "forced": forced})

    latency = query_latency(args.query_runs)
    initial = [r["initial"] for r in reps]
    rerun = [r["rerun"] for r in reps]
    forced = [r["forced"] for r in reps]

    def per_step(runs: list[dict]) -> dict[str, dict[str, float]]:
        return {step: summarise([r["steps_s"][step] for r in runs]) for step in runs[0]["steps_s"]}

    aggregate: dict[str, Any] = {
        "initial_total_s": summarise([r["total_s"] for r in initial]),
        "rerun_total_s": summarise([r["total_s"] for r in rerun]),
        "forced_total_s": summarise([r["total_s"] for r in forced]),
        "dbt_build_s": summarise([r["steps_s"]["dbt_build"] for r in initial]),
        "dbt_test_s": summarise([r["steps_s"]["dbt_test"] for r in initial]),
        "ingest_rows_per_s": summarise(
            [r["rows_ingested"] / r["steps_s"]["ingest_raw"] for r in initial]
        ),
        "staging_rows_per_s": summarise(
            [r["rows_staged_in"] / r["steps_s"]["load_staging"] for r in initial]
        ),
        "initial_steps": per_step(initial),
        "rerun_steps": per_step(rerun),
        "forced_steps": per_step(forced),
    }
    results = {
        "environment": environment(),
        "repetitions": reps,
        "aggregate": aggregate,
        "database_size": size,
        "query_latency": latency,
        "deterministic": all(fp == fingerprints[0] for fp in fingerprints),
        "published_fingerprints": fingerprints[0],
    }
    (ROOT / "benchmark").mkdir(exist_ok=True)
    (ROOT / "benchmark" / "results.json").write_text(
        json.dumps(results, indent=2) + "\n", encoding="utf-8"
    )
    doc, readme_block = render(results)
    (ROOT / "docs" / "benchmark.md").write_text(doc, encoding="utf-8")
    if args.update_readme:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        start, end = readme.index(README_START), readme.index(README_END)
        readme = readme[: start + len(README_START)] + "\n" + readme_block + "\n" + readme[end:]
        (ROOT / "README.md").write_text(readme, encoding="utf-8")
    print(readme_block)
    skipped = not any(r["rebuild"] for r in rerun)
    return 0 if results["deterministic"] and skipped else 1


if __name__ == "__main__":
    sys.exit(main())
