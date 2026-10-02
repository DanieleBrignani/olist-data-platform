"""Every dbt test is classified, and dbt's own severity agrees with the quality gate.

Parses the dbt project (no database needed) and inspects manifest.json, so a developer
running `dbt test` sees the same pass/warn/fail outcome the gate will compute.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from olist_platform.transform.dbt_runner import run_dbt


@pytest.fixture(scope="module")
def tests_in_manifest(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict]:
    target = tmp_path_factory.mktemp("dbt_parse")
    run_dbt(["parse"], target_path=str(target))
    manifest = json.loads((Path(target) / "manifest.json").read_text(encoding="utf-8"))
    return {uid: n for uid, n in manifest["nodes"].items() if n["resource_type"] == "test"}


def test_every_test_is_classified(tests_in_manifest: dict[str, dict]) -> None:
    for uid, node in tests_in_manifest.items():
        assert node["config"]["meta"].get("dq_severity") in {"WARNING", "ERROR", "CRITICAL"}, uid


def test_every_test_stores_failing_rows(tests_in_manifest: dict[str, dict]) -> None:
    for uid, node in tests_in_manifest.items():
        assert node["config"]["store_failures"] is True, uid
        assert node["config"]["schema"] == "dq_failures", uid


def test_dbt_severity_mirrors_dq_severity(tests_in_manifest: dict[str, dict]) -> None:
    for uid, node in tests_in_manifest.items():
        cfg = node["config"]
        dq = cfg["meta"]["dq_severity"]
        tolerance = int(cfg["meta"].get("tolerance", 0))
        if dq == "WARNING":
            assert cfg["severity"].lower() == "warn", uid
        elif dq == "CRITICAL":
            assert cfg["severity"].lower() == "error", uid
            assert cfg["error_if"] == "!= 0", uid
        else:  # ERROR: fails only beyond its tolerance
            assert cfg["severity"].lower() == "error", uid
            expected = f">{tolerance}" if tolerance else "!= 0"
            assert cfg["error_if"] == expected, uid


def test_required_rule_coverage(tests_in_manifest: dict[str, dict]) -> None:
    """Each mandatory data-quality rule exists as a classified test."""
    names = {n["name"] for n in tests_in_manifest.values()}

    def has(fragment: str) -> bool:
        return any(fragment in name for name in names)

    assert has("unique_fct_orders_order_id")
    assert has("relationships_fct_orders_customer_unique_id")
    assert has("relationships_fct_order_items_product_id")
    assert has("relationships_fct_order_items_seller_id")
    assert has("accepted_range_fct_payments_payment_value")
    assert has("accepted_range_fct_order_items_freight_value")
    assert has("accepted_values_fct_reviews_review_score")
    assert has("timeline_is_ordered_fct_orders_delivered_to_customer_at")
    assert has("not_in_future_fct_orders_purchased_at")
    assert has("assert_src_to_warehouse_reconciliation")
    severities = {n["config"]["meta"]["dq_severity"] for n in tests_in_manifest.values()}
    assert severities == {"WARNING", "ERROR", "CRITICAL"}
