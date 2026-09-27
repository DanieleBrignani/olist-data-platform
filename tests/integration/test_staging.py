"""raw -> src staging: typing, rule severities, quarantine vs flag, cascades, atomicity.

Every test builds a small synthetic dataset (tests/synthetic.py), injects ONE controlled
defect, ingests it with the real contracts and asserts where each record ended up.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from olist_platform.config import Role
from olist_platform.database.engine import get_engine
from olist_platform.errors import DataContractError
from olist_platform.ingestion.manifest import Lock, LockEntry, build_manifest
from olist_platform.ingestion.pipeline import run_ingestion, run_staging
from olist_platform.validation.contracts import Contract, load_contracts
from tests.synthetic import Rows, base_rows, cust, order, prod, seller, write_dataset

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]

CONTRACTS = load_contracts()
# A 3-row dataset cannot satisfy a 1% reject limit with a single injected defect; the limit
# itself is tested separately with the real contracts.
RELAXED = {t: c.model_copy(update={"max_reject_ratio": 1.0}) for t, c in CONTRACTS.items()}


def load(
    tmp_path: Path, rows: Rows, contracts: dict[str, Contract] = RELAXED, name: str = "data"
) -> None:
    directory = write_dataset(tmp_path / name, rows, contracts)
    entries = build_manifest(directory, contracts)
    lock = Lock(
        dataset="synthetic",
        dataset_version=2,
        files=[LockEntry(**e.model_dump(include=set(LockEntry.model_fields))) for e in entries],
    )
    engine = get_engine(Role.PIPELINE)
    run_ingestion(engine, directory, contracts, environment="test", lock=lock)
    run_staging(engine, contracts, environment="test")


def q(sql: str, **params):
    with get_engine(Role.ADMIN).connect() as conn:
        return conn.execute(text(sql), params).all()


def issues(table: str | None = None) -> list[tuple[str, str, str, str]]:
    """(table, rule, severity, action) for src-layer issues."""
    where = "AND source_table = :t" if table else ""
    return q(
        f"SELECT source_table, rule_name, severity, action FROM meta.rejected_records "
        f"WHERE layer = 'src' {where} ORDER BY 1, 2",
        t=table,
    )


def src_count(table: str) -> int:
    return q(f"SELECT count(*) FROM src.{table}")[0][0]


def assert_reconciles() -> None:
    """raw records = sum(source_record_count) + distinct quarantined records, per table."""
    for table in CONTRACTS:
        raw = q(
            f"SELECT count(*) FROM raw.{table} r JOIN meta.current_source_files f "
            f"ON f.source_file_id = r._source_file_id"
        )[0][0]
        kept = q(f"SELECT coalesce(sum(source_record_count), 0) FROM src.{table}")[0][0]
        quarantined = q(
            "SELECT count(DISTINCT source_row_number) FROM meta.rejected_records "
            "WHERE layer = 'src' AND action = 'quarantined' AND source_table = :t "
            "AND pipeline_run_id = (SELECT pipeline_run_id FROM meta.pipeline_runs "
            "WHERE flow_name = 'olist_staging' ORDER BY started_at DESC LIMIT 1)",
            t=table,
        )[0][0]
        assert raw == kept + quarantined, table


def test_clean_dataset_is_fully_typed_with_no_issues(tmp_path: Path) -> None:
    load(tmp_path, base_rows())
    assert issues() == []
    assert {t: src_count(t) for t in ("orders", "order_items", "geolocation")} == {
        "orders": 3,
        "order_items": 3,
        "geolocation": 4,
    }
    ts, price = q(
        "SELECT o.order_purchase_timestamp, i.price FROM src.orders o "
        "JOIN src.order_items i USING (order_id) WHERE o.order_id = :o",
        o=order(1),
    )[0]
    assert (ts.isoformat(), str(price)) == ("2018-01-01T10:00:00", "59.90")
    assert_reconciles()


def test_empty_string_becomes_null_and_misspelled_columns_are_renamed(tmp_path: Path) -> None:
    rows = base_rows()
    rows["products"][1]["product_category_name"] = ""
    load(tmp_path, rows)
    assert q(
        "SELECT product_category_name, product_name_length FROM src.products WHERE product_id = :p",
        p=prod(2),
    ) == [(None, 40)]
    assert q("SELECT review_comment_title FROM src.order_reviews LIMIT 1") == [(None,)]


def test_uncastable_value_is_quarantined_with_reason(tmp_path: Path) -> None:
    rows = base_rows()
    rows["order_items"][0]["price"] = "59,90"  # decimal comma
    load(tmp_path, rows)
    assert issues("order_items") == [("order_items", "type__price", "ERROR", "quarantined")]
    assert src_count("order_items") == 2
    reason, ref, raw = q(
        "SELECT reason, record_ref, raw_record->>'price' FROM "
        "meta.rejected_records WHERE layer = 'src'"
    )[0]
    assert "not a valid numeric: 59,90" in reason
    assert ref == "olist_order_items_dataset.csv#record=1"
    assert raw == "59,90"
    assert_reconciles()


def test_quarantined_parent_cascades_to_children(tmp_path: Path) -> None:
    rows = base_rows()
    rows["orders"][0]["order_purchase_timestamp"] = ""  # mandatory -> order 1 quarantined
    load(tmp_path, rows)
    assert set(issues()) == {
        ("orders", "not_null__order_purchase_timestamp", "ERROR", "quarantined"),
        ("order_items", "foreign_key__order_id", "ERROR", "quarantined"),
        ("order_payments", "foreign_key__order_id", "ERROR", "quarantined"),
        ("order_reviews", "foreign_key__order_id", "ERROR", "quarantined"),
    }
    assert q("SELECT count(*) FROM src.order_items WHERE order_id = :o", o=order(1)) == [(0,)]
    reason = q(
        "SELECT reason FROM meta.rejected_records WHERE source_table = 'order_items' "
        "AND layer = 'src'"
    )[0][0]
    assert "not found in orders (missing or quarantined)" in reason
    assert_reconciles()


@pytest.mark.parametrize(
    ("table", "column", "value", "rule"),
    [
        ("orders", "order_status", "lost", "order_status_accepted"),
        ("order_reviews", "review_score", "7", "review_score_in_range"),
        ("order_payments", "payment_value", "-1.00", "payment_value_non_negative"),
        ("order_items", "freight_value", "-0.01", "freight_non_negative"),
        (
            "orders",
            "order_delivered_customer_date",
            "2017-12-31 00:00:00",
            "delivered_not_before_purchase",
        ),
        ("orders", "order_purchase_timestamp", "2999-01-01 00:00:00", "purchase_not_in_future"),
        ("customers", "customer_state", "XX", "customer_state_is_uf"),
    ],
)
def test_error_rules_quarantine(
    tmp_path: Path, table: str, column: str, value: str, rule: str
) -> None:
    rows = base_rows()
    rows[table][0][column] = value
    load(tmp_path, rows)
    assert (table, rule, "ERROR", "quarantined") in issues(table)
    assert src_count(table) == len(rows[table]) - 1
    assert_reconciles()


def test_warning_rule_flags_but_keeps_the_record(tmp_path: Path) -> None:
    rows = base_rows()
    rows["order_payments"][0]["payment_type"] = "not_defined"
    load(tmp_path, rows)
    assert issues() == [("order_payments", "payment_type_defined", "WARNING", "flagged")]
    assert src_count("order_payments") == 3


def test_null_never_violates_an_expression_rule(tmp_path: Path) -> None:
    rows = base_rows()
    rows["orders"][0]["order_delivered_customer_date"] = ""
    rows["orders"][0]["order_status"] = "shipped"
    load(tmp_path, rows)
    assert issues() == []


def test_unexpected_exact_duplicate_is_flagged_and_collapsed(tmp_path: Path) -> None:
    rows = base_rows()
    rows["sellers"].append(dict(rows["sellers"][0]))
    load(tmp_path, rows)
    assert issues() == [("sellers", "exact_duplicate", "WARNING", "flagged")]
    assert q("SELECT source_record_count FROM src.sellers WHERE seller_id = :s", s=seller(1)) == [
        (2,)
    ]
    assert_reconciles()


def test_expected_geolocation_duplicates_are_collapsed_silently_but_counted(
    tmp_path: Path,
) -> None:
    rows = base_rows()
    rows["geolocation"] += [dict(rows["geolocation"][0]) for _ in range(3)]
    load(tmp_path, rows)
    assert issues() == []
    assert src_count("geolocation") == 4
    assert q("SELECT max(source_record_count) FROM src.geolocation") == [(4,)]
    details = q(
        "SELECT details->>'duplicates_collapsed' FROM meta.ingestion_events "
        "WHERE task = 'load_staging' AND source = 'geolocation'"
    )
    assert details == [("3",)]
    assert_reconciles()


def test_key_conflict_quarantines_every_version_and_never_picks_a_winner(
    tmp_path: Path,
) -> None:
    rows = base_rows()
    rows["customers"].append(dict(rows["customers"][0], customer_city="rio de janeiro"))
    load(tmp_path, rows)
    assert q(
        "SELECT count(*) FROM meta.rejected_records WHERE rule_name = 'primary_key_conflict'"
    ) == [(2,)]
    assert q("SELECT count(*) FROM src.customers WHERE customer_id = :c", c=cust(1)) == [(0,)]
    # ...and the order of that customer cascades out of src too
    assert ("orders", "foreign_key__customer_id", "ERROR", "quarantined") in issues("orders")
    assert_reconciles()


def test_warning_foreign_key_flags_but_keeps(tmp_path: Path) -> None:
    rows = base_rows()
    rows["customers"][0]["customer_zip_code_prefix"] = "99999"
    load(tmp_path, rows)
    assert issues() == [
        ("customers", "foreign_key__customer_zip_code_prefix", "WARNING", "flagged")
    ]
    assert src_count("customers") == 3


def test_restaging_is_idempotent(tmp_path: Path) -> None:
    rows = base_rows()
    rows["order_payments"][0]["payment_type"] = "not_defined"
    load(tmp_path, rows)
    fingerprint = "SELECT md5(string_agg(o::text, '|' ORDER BY o.order_id)) FROM src.orders o"
    before = q(fingerprint)
    run_staging(get_engine(Role.PIPELINE), RELAXED, environment="test")
    assert q(fingerprint) == before
    assert src_count("order_payments") == 3


def test_reject_ratio_over_limit_leaves_src_untouched_but_keeps_evidence(
    tmp_path: Path,
) -> None:
    load(tmp_path, base_rows(), CONTRACTS, name="good")
    rows = base_rows()
    rows["sellers"][0]["seller_state"] = "XX"  # 1 of 2 sellers = 50% > 1%

    with pytest.raises(DataContractError, match="reject ratio over contract limit"):
        load(tmp_path, rows, CONTRACTS, name="bad")

    assert src_count("sellers") == 2  # previous good src still published
    assert q("SELECT seller_state FROM src.sellers WHERE seller_id = :s", s=seller(1)) == [("SP",)]
    assert ("sellers", "seller_state_is_uf", "ERROR", "quarantined") in issues("sellers")
    # seller 1 is quarantined, so its order item cascades out too: 1 of 3 items = 33% > 1%
    assert q(
        "SELECT source, status, details->>'src_published' FROM meta.ingestion_events "
        "WHERE event = 'table_rejected' ORDER BY source"
    ) == [("order_items", "failed", "false"), ("sellers", "failed", "false")]
    assert q("SELECT failed_task, error_type FROM meta.pipeline_runs WHERE status = 'failed'") == [
        ("load_staging", "data_contract_violation")
    ]


def test_src_columns_match_contract_target_names() -> None:
    for table, contract in CONTRACTS.items():
        columns = [
            r[0]
            for r in q(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'src' "
                "AND table_name = :t AND column_name NOT LIKE '\\_%' "
                "AND column_name <> 'source_record_count' ORDER BY ordinal_position",
                t=table,
            )
        ]
        assert columns == [c.target_name or c.name for c in contract.columns], table
