"""Postgres (src) -> dbt: the real dbt project on a synthetic dataset whose business answers
can be computed by hand, so every metric definition is pinned by a test.

Scenario (all January 2018 unless stated):
  o1  person A, delivered on time                    items 59.90 + 12.50 freight
  o2  person B, delivered LATE (25th vs est. 20th)   items 59.90 + 12.50
  o3  person C, CANCELED                             items 59.90 + 12.50 (excluded from GMV)
  o4  person A again (new customer_id), February, delivered, voucher-paid, 100.00 + 10.00
  o5  person D, UNAVAILABLE, no items, no payment
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from olist_platform.config import Role
from olist_platform.database.engine import get_engine
from olist_platform.ingestion.manifest import Lock, LockEntry, build_manifest
from olist_platform.ingestion.pipeline import run_ingestion, run_staging
from olist_platform.transform.dbt_runner import run_dbt
from olist_platform.validation.contracts import load_contracts
from tests.synthetic import base_rows, cust, hexid, order, prod, seller, write_dataset

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]

CONTRACTS = load_contracts()


def scenario() -> dict:
    rows = base_rows()
    o2, o3 = rows["orders"][1], rows["orders"][2]
    o2["order_delivered_customer_date"] = "2018-01-25 12:00:00"
    o3.update(
        order_status="canceled", order_delivered_carrier_date="", order_delivered_customer_date=""
    )
    rows["customers"] += [
        {
            "customer_id": cust(4),
            "customer_unique_id": hexid(9, 1),  # person A again
            "customer_zip_code_prefix": "01001",
            "customer_city": "sao paulo",
            "customer_state": "SP",
        },
        {
            "customer_id": cust(5),
            "customer_unique_id": hexid(9, 5),
            "customer_zip_code_prefix": "01002",
            "customer_city": "São Paulo",
            "customer_state": "SP",
        },
    ]
    rows["orders"] += [
        {
            "order_id": order(4),
            "customer_id": cust(4),
            "order_status": "delivered",
            "order_purchase_timestamp": "2018-02-05 10:00:00",
            "order_approved_at": "2018-02-05 10:30:00",
            "order_delivered_carrier_date": "2018-02-06 09:00:00",
            "order_delivered_customer_date": "2018-02-09 15:00:00",
            "order_estimated_delivery_date": "2018-02-20 00:00:00",
        },
        {
            "order_id": order(5),
            "customer_id": cust(5),
            "order_status": "unavailable",
            "order_purchase_timestamp": "2018-01-15 10:00:00",
            "order_approved_at": "",
            "order_delivered_carrier_date": "",
            "order_delivered_customer_date": "",
            "order_estimated_delivery_date": "2018-01-30 00:00:00",
        },
    ]
    rows["order_items"].append(
        {
            "order_id": order(4),
            "order_item_id": "1",
            "product_id": prod(2),
            "seller_id": seller(1),
            "shipping_limit_date": "2018-02-08 00:00:00",
            "price": "100.00",
            "freight_value": "10.00",
        }
    )
    rows["order_payments"].append(
        {
            "order_id": order(4),
            "payment_sequential": "1",
            "payment_type": "voucher",
            "payment_installments": "1",
            "payment_value": "110.00",
        }
    )
    return rows


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> None:
    tmp = tmp_path_factory.mktemp("dbt")
    with get_engine(Role.ADMIN).begin() as conn:  # module-scoped: clean once here
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
    directory = write_dataset(tmp / "data", scenario(), CONTRACTS)
    entries = build_manifest(directory, CONTRACTS)
    lock = Lock(
        dataset="synthetic",
        dataset_version=2,
        files=[LockEntry(**e.model_dump(include=set(LockEntry.model_fields))) for e in entries],
    )
    engine = get_engine(Role.PIPELINE)
    run_ingestion(engine, directory, CONTRACTS, environment="test", lock=lock)
    run_staging(engine, CONTRACTS, environment="test")
    result = run_dbt(["build"], target_path=str(tmp / "target"))
    assert result.success


def q(sql: str, **params):
    # build schemas are owned by olist_pipeline; admin (not a superuser) cannot read them
    with get_engine(Role.PIPELINE).connect() as conn:
        return conn.execute(text(sql), params).all()


@pytest.mark.usefixtures("built")
class TestWarehouse:
    def test_fact_grains(self) -> None:
        assert q("SELECT count(*) FROM warehouse_build.fct_orders") == [(5,)]
        assert q("SELECT count(*) FROM warehouse_build.fct_order_items") == [(4,)]
        assert q("SELECT count(*) FROM warehouse_build.fct_payments") == [(4,)]
        assert q("SELECT count(*) FROM warehouse_build.fct_reviews") == [(3,)]

    def test_customer_dimension_is_per_person(self) -> None:
        assert q("SELECT count(*) FROM warehouse_build.dim_customer") == [(4,)]
        assert q(
            "SELECT is_repeat_customer FROM warehouse_build.dim_customer "
            "WHERE customer_unique_id = :p",
            p=hexid(9, 1),
        ) == [(True,)]

    def test_city_names_are_normalised_in_geography(self) -> None:
        assert q(
            "SELECT DISTINCT city FROM warehouse_build.dim_geography "
            "WHERE zip_code_prefix = '01002'"
        ) == [("sao paulo",)]

    def test_late_flag_and_lead_time(self) -> None:
        rows = dict(q("SELECT order_id, is_late FROM warehouse_build.fct_orders"))
        assert rows[order(1)] is False
        assert rows[order(2)] is True
        assert rows[order(3)] is None  # never delivered
        assert q(
            "SELECT days_vs_estimate FROM warehouse_build.fct_orders WHERE order_id = :o",
            o=order(2),
        ) == [(5,)]

    def test_order_without_items_has_zero_value(self) -> None:
        assert q(
            "SELECT item_count, order_value, payment_value FROM "
            "warehouse_build.fct_orders WHERE order_id = :o",
            o=order(5),
        ) == [(0, Decimal("0.00"), Decimal("0.00"))]


@pytest.mark.usefixtures("built")
class TestMarts:
    def test_sales_gmv_excludes_canceled_and_aov_uses_orders_with_items(self) -> None:
        rows = q(
            "SELECT year_month, orders, canceled_orders, gmv, avg_order_value, "
            "payment_volume FROM marts_build.mart_sales ORDER BY month_start"
        )
        assert rows == [
            ("2018-01", 4, 2, Decimal("144.80"), Decimal("72.40"), Decimal("217.20")),
            ("2018-02", 1, 0, Decimal("110.00"), Decimal("110.00"), Decimal("110.00")),
        ]

    def test_delivery_performance_counts_only_delivered(self) -> None:
        assert q(
            "SELECT delivered_orders, late_orders, late_rate FROM "
            "marts_build.mart_delivery_performance WHERE year_month = '2018-01'"
        ) == [(2, 1, Decimal("0.5000"))]

    def test_payment_methods(self) -> None:
        assert q(
            "SELECT payment_type, payment_volume FROM marts_build.mart_payment_methods "
            "WHERE year_month = '2018-02'"
        ) == [("voucher", Decimal("110.00"))]

    def test_customer_behavior_repeat_person(self) -> None:
        assert q(
            "SELECT valid_order_count, lifetime_value, days_first_to_last_order FROM "
            "marts_build.mart_customer_behavior WHERE customer_unique_id = :p",
            p=hexid(9, 1),
        ) == [(2, Decimal("182.40"), 35)]

    def test_category_uses_english_name(self) -> None:
        assert q(
            "SELECT category_name, orders, items_sold FROM marts_build.mart_category_performance"
        ) == [("health_beauty", 3, 3)]


@pytest.mark.usefixtures("built")
def test_reporting_role_cannot_read_unpublished_build_schemas() -> None:
    with get_engine(Role.ADMIN).connect() as conn:
        for schema in ("warehouse_build", "marts_build", "stg", "int"):
            allowed = conn.execute(
                text("SELECT has_schema_privilege('olist_reporting', :s, 'USAGE')"),
                {"s": schema},
            ).scalar_one()
            assert allowed is False, schema
