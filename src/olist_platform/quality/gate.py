"""Quality gate: decides whether the freshly built warehouse may be published (ADR-0004/0005).

Severity semantics (dbt tests carry `meta.dq_severity`, set in the dbt project):
* WARNING  -> recorded, never blocks.
* ERROR    -> blocks only when failing rows exceed the rule's `meta.tolerance`.
* CRITICAL -> blocks on ANY failing row (tolerance is always 0).
Additionally, a test that could not run (dbt status `error`/`skipped`) blocks when it is
ERROR or CRITICAL: an unverified critical rule is treated as a failed one.

`evaluate` is a pure function (unit-tested without a database); `persist_results` records the
evidence; `enforce` raises QualityGateError so the orchestrator stops before publish_marts.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from sqlalchemy import Engine, text

from olist_platform.errors import QualityGateError
from olist_platform.transform.dbt_runner import DbtRun
from olist_platform.utils.logging import get_logger
from olist_platform.validation.contracts import Severity

FAILING_ROWS_SAMPLE = 100

log = get_logger(__name__)


@dataclass(frozen=True)
class DqResult:
    test_unique_id: str
    test_name: str
    model: str | None
    severity: Severity
    status: str  # pass | warn | fail | error | skipped
    failures: int
    tolerance: int = 0
    message: str | None = None
    failures_relation: str | None = None
    duration_ms: float = 0.0

    @property
    def blocking(self) -> bool:
        if self.severity == Severity.WARNING:
            return False
        if self.status in ("error", "skipped"):
            return True
        allowed = 0 if self.severity == Severity.CRITICAL else self.tolerance
        return self.failures > allowed

    @property
    def failed(self) -> bool:
        return self.failures > 0 or self.status in ("error", "skipped")


@dataclass
class GateDecision:
    passed: bool
    evaluated: int
    blocking: list[DqResult] = field(default_factory=list)
    warnings: list[DqResult] = field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        return [
            f"{r.severity}: {r.test_name} on {r.model or '?'} "
            f"({r.status}, {r.failures} failing rows, tolerance {r.tolerance})"
            for r in self.blocking
        ]


def evaluate(results: list[DqResult]) -> GateDecision:
    blocking = [r for r in results if r.blocking]
    warnings = [r for r in results if r.failed and not r.blocking]
    return GateDecision(not blocking, len(results), blocking, warnings)


def results_from_dbt(run: DbtRun) -> list[DqResult]:
    """Convert dbt test node results into DqResults using each test's dq metadata."""
    results = []
    for node in run.nodes:
        if node.resource_type != "test":
            continue
        meta = node.meta or {}
        severity = Severity(meta.get("dq_severity", Severity.CRITICAL))  # unclassified = strict
        tolerance = 0 if severity == Severity.CRITICAL else int(meta.get("tolerance", 0))
        results.append(
            DqResult(
                test_unique_id=node.unique_id,
                test_name=node.name,
                model=node.attached_model,
                severity=severity,
                status=node.status,
                failures=int(node.failures or 0),
                tolerance=tolerance,
                message=node.message,
                failures_relation=node.relation_name,
                duration_ms=round(node.execution_time * 1000, 1),
            )
        )
    return results


def persist_results(
    engine: Engine, run_id: uuid.UUID, results: list[DqResult], decision: GateDecision
) -> None:
    """Write dq_results, a sample of failing rows, and the gate decision in one transaction."""
    with engine.begin() as conn:
        for r in results:
            conn.execute(
                text(
                    "INSERT INTO meta.dq_results (pipeline_run_id, test_unique_id, test_name, "
                    "model, severity, status, failures, tolerance, blocking, message, "
                    "failures_relation, duration_ms) "
                    "VALUES (:run, :uid, :name, :model, :sev, :status, :failures, "
                    ":tol, :blocking, :msg, :rel, :ms)"
                ),
                {
                    "run": run_id,
                    "uid": r.test_unique_id,
                    "name": r.test_name,
                    "model": r.model,
                    "sev": r.severity.value,
                    "status": r.status,
                    "failures": r.failures,
                    "tol": r.tolerance,
                    "blocking": r.blocking,
                    "msg": r.message,
                    "rel": r.failures_relation,
                    "ms": r.duration_ms,
                },
            )
            if r.failures > 0 and r.failures_relation:
                # Relation name comes from the dbt manifest (store_failures), not user input
                conn.execute(
                    text(
                        "INSERT INTO meta.rejected_records (pipeline_run_id, source_table, "
                        "record_ref, rule_name, severity, reason, raw_record, layer, action) "
                        "SELECT :run, :model, "
                        ":model || ':' || :name || '#' || row_number() OVER (), "
                        ":name, :sev, :reason, to_jsonb(f), 'warehouse', 'reported' "
                        f"FROM (SELECT * FROM {r.failures_relation} LIMIT {FAILING_ROWS_SAMPLE}) f"
                    ),
                    {
                        "run": run_id,
                        "model": r.model or "unknown",
                        "name": r.test_name,
                        "sev": r.severity.value,
                        "reason": f"{r.failures} rows fail {r.test_name} "
                        f"(tolerance {r.tolerance}); sample of up to {FAILING_ROWS_SAMPLE}",
                    },
                )
        conn.execute(
            text(
                "INSERT INTO meta.quality_gate_decisions (pipeline_run_id, decision, "
                "tests_evaluated, blocking_failures, warnings, reasons) "
                "VALUES (:run, :decision, :n, :blocking, :warnings, CAST(:reasons AS jsonb))"
            ),
            {
                "run": run_id,
                "decision": "PASS" if decision.passed else "FAIL",
                "n": decision.evaluated,
                "blocking": len(decision.blocking),
                "warnings": len(decision.warnings),
                "reasons": json.dumps(decision.reasons),
            },
        )

    for r in decision.warnings:
        log.warning(
            "dq_rule_failed",
            task="quality_gate",
            rule=r.test_name,
            model=r.model,
            severity=r.severity.value,
            failures=r.failures,
            tolerance=r.tolerance,
            blocking=False,
        )
    for r in decision.blocking:
        log.error(
            "dq_rule_failed",
            task="quality_gate",
            rule=r.test_name,
            model=r.model,
            severity=r.severity.value,
            failures=r.failures,
            tolerance=r.tolerance,
            blocking=True,
            status=r.status,
        )
    log.info(
        "quality_gate_decision",
        task="quality_gate",
        decision="PASS" if decision.passed else "FAIL",
        tests=decision.evaluated,
        blocking_failures=len(decision.blocking),
        warnings=len(decision.warnings),
    )


def enforce(decision: GateDecision) -> None:
    if not decision.passed:
        raise QualityGateError(
            "quality gate FAILED; publication blocked: " + "; ".join(decision.reasons)
        )
