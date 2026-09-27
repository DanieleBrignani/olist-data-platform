from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from olist_platform.validation.contracts import (
    CONTRACTS_DIR,
    Contract,
    ExpressionRule,
    Severity,
    load_contracts,
    validate_references,
)
from olist_platform.validation.docs import render

# Files published by Kaggle for olistbr/brazilian-ecommerce v2 (datasets/list API)
KAGGLE_V2_FILES = {
    "olist_customers_dataset.csv",
    "olist_geolocation_dataset.csv",
    "olist_order_items_dataset.csv",
    "olist_order_payments_dataset.csv",
    "olist_order_reviews_dataset.csv",
    "olist_orders_dataset.csv",
    "olist_products_dataset.csv",
    "olist_sellers_dataset.csv",
    "product_category_name_translation.csv",
}


@pytest.fixture(scope="module")
def contracts() -> dict[str, Contract]:
    return load_contracts()


@pytest.fixture
def orders_doc() -> dict[str, Any]:
    return yaml.safe_load((CONTRACTS_DIR / "orders.yml").read_text(encoding="utf-8"))


def test_one_contract_per_source_file(contracts: dict[str, Contract]) -> None:
    assert {c.file for c in contracts.values()} == KAGGLE_V2_FILES


def test_all_contracts_pin_dataset_version_2(contracts: dict[str, Contract]) -> None:
    assert {c.dataset_version for c in contracts.values()} == {2}


def test_every_column_is_documented_and_typed(contracts: dict[str, Contract]) -> None:
    for contract in contracts.values():
        for column in contract.columns:
            assert column.description.strip(), f"{contract.table}.{column.name}"


def test_identifier_columns_are_hex_patterns(contracts: dict[str, Contract]) -> None:
    for contract in contracts.values():
        for column in contract.columns:
            if column.name.endswith("_id") and column.name != "order_item_id":
                assert column.pattern == "^[0-9a-f]{32}$", f"{contract.table}.{column.name}"


def test_zip_prefixes_are_text_to_keep_leading_zeros(contracts: dict[str, Contract]) -> None:
    zip_columns = [
        c for contract in contracts.values() for c in contract.columns if "zip_code" in c.name
    ]
    assert zip_columns and all(c.type == "text" for c in zip_columns)


def test_review_primary_key_is_composite(contracts: dict[str, Contract]) -> None:
    # review_id alone is not unique in the source (docs/source_profile.md)
    assert contracts["order_reviews"].primary_key == ["review_id", "order_id"]


def test_spec_mandated_rules_exist(contracts: dict[str, Contract]) -> None:
    rules = {(c.table, r.name): r for c in contracts.values() for r in c.rules}
    assert rules[("order_payments", "payment_value_non_negative")].severity == Severity.ERROR
    assert rules[("order_items", "freight_non_negative")].severity == Severity.ERROR
    assert rules[("order_reviews", "review_score_in_range")].severity == Severity.ERROR
    assert rules[("orders", "delivered_not_before_purchase")].severity == Severity.ERROR
    assert isinstance(rules[("orders", "purchase_not_in_future")], ExpressionRule)


def test_valid_contract_round_trips(orders_doc: dict[str, Any]) -> None:
    assert Contract.model_validate(orders_doc).table == "orders"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.update(primary_key=["nope"]), "unknown columns"),
        (lambda d: d["columns"].append(copy.deepcopy(d["columns"][0])), "duplicate column"),
        (lambda d: d["columns"][0].update(nullable=True), "must be non-nullable"),
        (lambda d: d["columns"][0].update(type="varchar"), "type"),
        (lambda d: d["columns"][0].update(description=""), "description"),
        (lambda d: d["rules"][0].update(severity="FATAL"), "severity"),
        (lambda d: d["rules"][0].update(column="missing_col"), "unknown columns"),
        (lambda d: d.update(max_reject_ratio=1.5), "max_reject_ratio"),
        (lambda d: d.update(surprise_field=True), "surprise_field"),
        (lambda d: d.update(table="orders; DROP TABLE x"), "identifier"),
    ],
)
def test_invalid_contracts_are_rejected(orders_doc: dict[str, Any], mutate, message: str) -> None:
    mutate(orders_doc)
    with pytest.raises(ValidationError, match=message):
        Contract.model_validate(orders_doc)


def test_foreign_key_to_unknown_table_is_rejected(contracts: dict[str, Contract]) -> None:
    without_customers = {k: v for k, v in contracts.items() if k != "customers"}
    with pytest.raises(ValueError, match="unknown table customers"):
        validate_references(without_customers)


def test_foreign_key_type_mismatch_is_rejected(contracts: dict[str, Contract]) -> None:
    customers = contracts["customers"]
    broken_col = customers.column("customer_id").model_copy(update={"type": "integer"})
    broken = customers.model_copy(update={"columns": [broken_col, *customers.columns[1:]]})
    with pytest.raises(ValueError, match="type mismatch"):
        validate_references({**contracts, "customers": broken})


def test_generated_contract_docs_are_up_to_date(contracts: dict[str, Contract]) -> None:
    committed = Path(CONTRACTS_DIR.parent / "docs" / "data_contracts.md").read_text(
        encoding="utf-8"
    )
    assert committed == render(contracts), "run `olist contracts docs` and commit the result"
