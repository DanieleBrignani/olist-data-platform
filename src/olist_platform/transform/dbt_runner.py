"""Programmatic dbt invocation (dbtRunner) with structured results.

Used by the CLI (`olist transform`) and, in Phase 8, by the orchestrated flow. dbt connects
as olist_pipeline using dbt/profiles.yml (credentials from the environment only).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dbt.cli.main import dbtRunner

from olist_platform.errors import DeterministicError
from olist_platform.utils.logging import get_logger

DBT_DIR = Path(__file__).resolve().parents[3] / "dbt"

log = get_logger(__name__)


class DbtError(DeterministicError):
    """A dbt model or test failed (same inputs fail the same way)."""

    error_type = "dbt_failed"


@dataclass(frozen=True)
class NodeResult:
    unique_id: str
    resource_type: str
    status: str
    execution_time: float
    message: str | None
    failures: int | None
    name: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    attached_model: str | None = None  # model a generic test is attached to
    relation_name: str | None = None  # store_failures table for tests


def _attached_model(node: Any) -> str | None:
    attached = getattr(node, "attached_node", None)
    if attached:
        return attached.split(".")[-1]
    meta_model = (getattr(node.config, "meta", None) or {}).get("model")
    return meta_model


@dataclass
class DbtRun:
    command: list[str]
    success: bool
    duration_ms: float
    nodes: list[NodeResult] = field(default_factory=list)

    def by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for n in self.nodes:
            counts[n.status] = counts.get(n.status, 0) + 1
        return counts


def run_dbt(
    args: list[str], *, target_path: str | None = None, raise_on_failure: bool = True
) -> DbtRun:
    target = target_path or os.environ.get("DBT_TARGET_PATH") or str(DBT_DIR / "target")
    command = [
        *args,
        "--project-dir",
        str(DBT_DIR),
        "--profiles-dir",
        str(DBT_DIR),
        "--target-path",
        target,
    ]
    started = time.perf_counter()
    result = dbtRunner().invoke(command)
    duration_ms = round((time.perf_counter() - started) * 1000, 1)

    nodes: list[NodeResult] = []
    if result.result is not None and hasattr(result.result, "results"):
        for r in result.result.results:
            nodes.append(
                NodeResult(
                    unique_id=r.node.unique_id,
                    resource_type=str(r.node.resource_type),
                    status=str(r.status),
                    execution_time=round(r.execution_time or 0.0, 3),
                    message=r.message,
                    failures=r.failures,
                    name=r.node.name,
                    meta=dict(getattr(r.node.config, "meta", None) or {}),
                    attached_model=_attached_model(r.node),
                    relation_name=getattr(r.node, "relation_name", None),
                )
            )
    run = DbtRun(args, bool(result.success), duration_ms, nodes)
    log.info(
        "dbt_invocation",
        command=" ".join(args),
        status="ok" if run.success else "failed",
        duration_ms=duration_ms,
        node_statuses=run.by_status(),
    )

    if not run.success and raise_on_failure:
        failed = [n.unique_id for n in nodes if n.status in ("error", "fail")]
        detail = failed or [str(result.exception)]
        raise DbtError(f"dbt {' '.join(args)} failed: {detail}")
    return run
