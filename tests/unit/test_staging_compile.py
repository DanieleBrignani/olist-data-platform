from __future__ import annotations

import pytest

from olist_platform.database.staging import (
    issues_insert_sql,
    lit,
    rule_checks,
    text_checks,
    topological_order,
)
from olist_platform.errors import DataContractError
from olist_platform.validation.contracts import Reference, load_contracts

CONTRACTS = load_contracts()


def test_parents_are_staged_before_children() -> None:
    order = [c.table for c in topological_order(CONTRACTS)]
    for contract in CONTRACTS.values():
        for fk in contract.foreign_keys:
            assert order.index(fk.references.table) < order.index(contract.table)


def test_foreign_key_cycle_is_rejected() -> None:
    customers = CONTRACTS["customers"]
    fk = customers.foreign_keys[0].model_copy(
        update={
            "references": Reference(table="orders", columns=["customer_id"]),
            "columns": ["customer_id"],
        }
    )
    cyclic = {**CONTRACTS, "customers": customers.model_copy(update={"foreign_keys": [fk]})}
    with pytest.raises(DataContractError, match="cycle"):
        topological_order(cyclic)


def test_literal_escaping() -> None:
    assert lit("it's") == "'it''s'"


def test_every_non_text_column_gets_a_type_check() -> None:
    items = CONTRACTS["order_items"]
    names = {c.rule_name for c in text_checks(items)}
    assert {
        "type__price",
        "type__freight_value",
        "type__order_item_id",
        "type__shipping_limit_date",
    } <= names
    assert "type__order_id" not in names  # text columns are never cast


def test_rule_checks_cover_every_contract_rule() -> None:
    for contract in CONTRACTS.values():
        assert [c.rule_name for c in rule_checks(contract)] == [r.name for r in contract.rules]


def test_expression_rules_treat_null_as_pass() -> None:
    check = next(
        c
        for c in rule_checks(CONTRACTS["orders"])
        if c.rule_name == "delivered_not_before_purchase"
    )
    assert check.violated_sql.endswith("IS FALSE)")


def test_generated_insert_prefilters_before_expansion() -> None:
    sql = issues_insert_sql(
        "sellers", text_checks(CONTRACTS["sellers"]), "raw.sellers", "s._source_file_id = :fid"
    )
    assert "WHERE (s._source_file_id = :fid) AND (COALESCE(" in sql
    assert sql.count("CROSS JOIN LATERAL") == 1
