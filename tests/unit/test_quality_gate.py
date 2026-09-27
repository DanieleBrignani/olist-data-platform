from __future__ import annotations

import pytest

from olist_platform.errors import QualityGateError
from olist_platform.quality.gate import DqResult, enforce, evaluate, results_from_dbt
from olist_platform.transform.dbt_runner import DbtRun, NodeResult
from olist_platform.validation.contracts import Severity


def r(severity: Severity, failures: int = 0, tolerance: int = 0, status: str = "pass") -> DqResult:
    return DqResult(f"test.{severity}.{failures}", "t", "m", severity, status, failures, tolerance)


@pytest.mark.parametrize(
    ("result", "blocking"),
    [
        (r(Severity.WARNING, failures=10_000, status="warn"), False),
        (r(Severity.WARNING, status="error"), False),
        (r(Severity.CRITICAL), False),
        (r(Severity.CRITICAL, failures=1, status="fail"), True),
        (r(Severity.CRITICAL, status="error"), True),  # could not run = not verified
        (r(Severity.CRITICAL, status="skipped"), True),
        (r(Severity.ERROR, failures=18, tolerance=500, status="warn"), False),
        (r(Severity.ERROR, failures=500, tolerance=500, status="warn"), False),
        (r(Severity.ERROR, failures=501, tolerance=500, status="fail"), True),
        (r(Severity.ERROR, failures=1, tolerance=0, status="fail"), True),
        (r(Severity.ERROR, status="error"), True),
    ],
)
def test_blocking_semantics(result: DqResult, blocking: bool) -> None:
    assert result.blocking is blocking


def test_critical_tolerance_is_ignored() -> None:
    assert r(Severity.CRITICAL, failures=1, tolerance=1000, status="fail").blocking


def test_gate_passes_with_only_warnings_and_tolerated_errors() -> None:
    decision = evaluate(
        [
            r(Severity.CRITICAL),
            r(Severity.WARNING, failures=3, status="warn"),
            r(Severity.ERROR, failures=18, tolerance=500, status="warn"),
        ]
    )
    assert decision.passed
    assert decision.evaluated == 3
    assert len(decision.warnings) == 2 and not decision.blocking
    enforce(decision)  # does not raise


def test_gate_fails_on_one_critical_and_explains_why() -> None:
    decision = evaluate(
        [
            r(Severity.WARNING, failures=3, status="warn"),
            r(Severity.CRITICAL, failures=2, status="fail"),
        ]
    )
    assert not decision.passed
    assert decision.reasons == ["CRITICAL: t on m (fail, 2 failing rows, tolerance 0)"]
    with pytest.raises(QualityGateError, match="publication blocked"):
        enforce(decision)


def _node(
    meta: dict, status: str = "fail", failures: int = 1, resource_type: str = "test"
) -> NodeResult:
    return NodeResult(
        "test.olist.x",
        resource_type,
        status,
        0.25,
        "msg",
        failures,
        name="x",
        meta=meta,
        attached_model="fct_orders",
        relation_name='"db"."dq_failures"."x"',
    )


def test_results_from_dbt_reads_classification_and_skips_models() -> None:
    run = DbtRun(
        ["test"],
        False,
        1.0,
        [
            _node({"dq_severity": "ERROR", "tolerance": 500}, status="warn", failures=18),
            _node({}, status="fail"),  # unclassified -> treated as CRITICAL
            _node({"dq_severity": "CRITICAL", "tolerance": 99}),  # tolerance ignored
            _node({}, resource_type="model"),
        ],
    )
    results = results_from_dbt(run)
    assert [(x.severity, x.tolerance) for x in results] == [
        (Severity.ERROR, 500),
        (Severity.CRITICAL, 0),
        (Severity.CRITICAL, 0),
    ]
    assert results[0].model == "fct_orders"
    assert results[0].duration_ms == 250.0
    assert not results[0].blocking and results[1].blocking and results[2].blocking
