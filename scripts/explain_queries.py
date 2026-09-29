"""EXPLAIN ANALYZE representative queries on src and on the published warehouse, with and
without the index under test.

For each query: warm-up run, then N measured runs WITH the index, then the same inside a
transaction that drops the index and is ROLLED BACK (the database is never modified).
Writes docs/query_plans.md. Every number in that file comes from this script.

Warehouse indexes are declared in dbt/models/marts/core/_core.yml; dbt names them by hash,
so they are addressed here as `schema.table(column)` and resolved through pg_indexes.

Usage (after a published run, e.g. `make pipeline`):
    docker compose run --rm dev python scripts/explain_queries.py [--runs 5]
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from olist_platform.config import Role
from olist_platform.database.engine import get_engine

OUT = Path(__file__).resolve().parents[1] / "docs" / "query_plans.md"


@dataclass(frozen=True)
class Query:
    key: str
    question: str
    index: str
    sql: str
    role: Role = Role.ADMIN  # must own the table to DROP INDEX (rolled back)


def resolve_index(conn: Connection, spec: str) -> str:
    """`schema.table(column)` -> qualified name of the single-column index on it."""
    if "(" not in spec:
        return spec
    relation, column = spec.rstrip(")").split("(")
    schema, table = relation.split(".")
    name = conn.execute(
        text(
            "SELECT indexname FROM pg_indexes WHERE schemaname = :s AND tablename = :t "
            "AND indexdef LIKE '%(' || :c || ')'"
        ),
        {"s": schema, "t": table, "c": column},
    ).scalar_one()
    return f'{schema}."{name}"'


def pick_parameters(conn: Connection) -> dict[str, Any]:
    """Deterministic, data-derived parameters (no hand-picked ids)."""
    seller = conn.execute(
        text(
            "SELECT seller_id FROM src.order_items GROUP BY seller_id "
            "ORDER BY count(*) DESC, seller_id LIMIT 1 OFFSET 49"
        )
    ).scalar_one()
    order = conn.execute(
        text(
            "SELECT order_id FROM src.order_reviews GROUP BY order_id "
            "ORDER BY count(*) DESC, order_id LIMIT 1"
        )
    ).scalar_one()
    return {"seller_id": seller, "order_id": order}


def pick_warehouse_parameters(conn: Connection) -> dict[str, Any]:
    def one(sql: str) -> Any:
        return conn.execute(text(sql)).scalar_one()

    return {
        "w_seller_id": one(
            "SELECT seller_id FROM warehouse.fct_order_items GROUP BY seller_id "
            "ORDER BY count(*) DESC, seller_id LIMIT 1 OFFSET 49"
        ),
        "w_product_id": one(
            "SELECT product_id FROM warehouse.fct_order_items GROUP BY product_id "
            "ORDER BY count(*) DESC, product_id LIMIT 1 OFFSET 49"
        ),
        "w_customer": one(
            "SELECT customer_unique_id FROM warehouse.fct_orders GROUP BY 1 "
            "ORDER BY count(*) DESC, 1 LIMIT 1"
        ),
        "w_order_id": one(
            "SELECT order_id FROM warehouse.fct_reviews GROUP BY order_id "
            "ORDER BY count(*) DESC, order_id LIMIT 1"
        ),
    }


def queries(p: dict[str, Any]) -> list[Query]:
    return [
        Query(
            "Q1 seller revenue by month",
            "Monthly revenue of one seller (50th largest by items sold)",
            "src.ix_src_order_items_seller_id",
            f"""SELECT date_trunc('month', o.order_purchase_timestamp) AS month,
       sum(i.price) AS revenue, count(*) AS items
FROM src.order_items i JOIN src.orders o USING (order_id)
WHERE i.seller_id = '{p["seller_id"]}'
GROUP BY 1 ORDER BY 1""",
        ),
        Query(
            "Q2 one week of orders",
            "Orders and average delivery lead time for one purchase week",
            "src.ix_src_orders_purchase_ts",
            """SELECT count(*) AS orders,
       avg(order_delivered_customer_date - order_purchase_timestamp) AS avg_lead_time
FROM src.orders
WHERE order_purchase_timestamp >= '2018-03-01' AND order_purchase_timestamp < '2018-03-08'""",
        ),
        Query(
            "Q3 revenue by month (all orders)",
            "Monthly revenue over the whole history (full aggregation)",
            "src.ix_src_orders_purchase_ts",
            """SELECT date_trunc('month', o.order_purchase_timestamp) AS month,
       sum(i.price + i.freight_value) AS gross_revenue
FROM src.orders o JOIN src.order_items i USING (order_id)
GROUP BY 1 ORDER BY 1""",
        ),
        Query(
            "Q4 reviews of one order",
            "All reviews attached to one order (FK lookup)",
            "src.ix_src_order_reviews_order_id",
            f"""SELECT review_id, review_score, review_answer_timestamp
FROM src.order_reviews WHERE order_id = '{p["order_id"]}'""",
        ),
        # ---- published warehouse: what the reporting role actually queries
        Query(
            "W1 seller sales history",
            "Monthly revenue of one seller from the fact (50th largest by items sold)",
            "warehouse.fct_order_items(seller_id)",
            f"""SELECT d.year_month, sum(f.price) AS revenue, count(*) AS items
FROM warehouse.fct_order_items f JOIN warehouse.dim_date d ON d.date_key = f.purchase_date_key
WHERE f.seller_id = '{p["w_seller_id"]}'
GROUP BY 1 ORDER BY 1""",
            Role.PIPELINE,
        ),
        Query(
            "W2 product sales history",
            "All sales of one product (50th best-selling)",
            "warehouse.fct_order_items(product_id)",
            f"""SELECT count(*) AS items, sum(price) AS revenue, avg(freight_value) AS avg_freight
FROM warehouse.fct_order_items WHERE product_id = '{p["w_product_id"]}'""",
            Role.PIPELINE,
        ),
        Query(
            "W3 customer order history",
            "Every order of one person (the person with the most orders)",
            "warehouse.fct_orders(customer_unique_id)",
            f"""SELECT order_id, purchase_date_key, order_value, is_late
FROM warehouse.fct_orders WHERE customer_unique_id = '{p["w_customer"]}'
ORDER BY purchase_date_key""",
            Role.PIPELINE,
        ),
        Query(
            "W4 one week of orders",
            "Orders and late share for one purchase week, filtered on the date key",
            "warehouse.fct_orders(purchase_date_key)",
            """SELECT count(*) AS orders, avg(is_late::int) AS late_share
FROM warehouse.fct_orders WHERE purchase_date_key BETWEEN 20180301 AND 20180307""",
            Role.PIPELINE,
        ),
        Query(
            "W5 reviews of one order",
            "All reviews attached to one order",
            "warehouse.fct_reviews(order_id)",
            f"""SELECT review_id, review_score
FROM warehouse.fct_reviews WHERE order_id = '{p["w_order_id"]}'""",
            Role.PIPELINE,
        ),
    ]


def walk(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [node, *(n for child in node.get("Plans", []) for n in walk(child))]


def measure(conn: Connection, sql: str, runs: int) -> dict[str, Any]:
    conn.execute(text(sql)).all()  # warm-up
    times, plan = [], None
    for _ in range(runs):
        raw = conn.execute(text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}")).scalar_one()
        doc = raw if isinstance(raw, list) else json.loads(raw)
        times.append(doc[0]["Execution Time"])
        plan = doc[0]["Plan"]
    nodes = walk(plan)
    text_plan = "\n".join(
        r[0] for r in conn.execute(text(f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF) {sql}"))
    )
    return {
        "median_ms": statistics.median(times),
        "min_ms": min(times),
        # Bitmap Index Scan nodes carry the index but no relation name; include them too
        "access": sorted(
            {
                n["Node Type"]
                + (f" on {n['Relation Name']}" if n.get("Relation Name") else "")
                + (f" using {n['Index Name']}" if n.get("Index Name") else "")
                for n in nodes
                if n.get("Relation Name") or n.get("Index Name")
            }
        ),
        "buffers": plan.get("Shared Hit Blocks", 0) + plan.get("Shared Read Blocks", 0),
        "plan": text_plan,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()

    engine = get_engine(Role.ADMIN)  # owner: may DROP INDEX inside a rolled-back transaction
    with engine.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT").execute(text("ANALYZE src.orders"))
        conn.execute(text("ANALYZE src.order_items"))
        conn.execute(text("ANALYZE src.order_reviews"))
        version = conn.execute(text("SHOW server_version")).scalar_one()
        counts = {
            t: conn.execute(text(f"SELECT count(*) FROM src.{t}")).scalar_one()
            for t in ("orders", "order_items", "order_reviews")
        }
        params = pick_parameters(conn)
    with get_engine(Role.PIPELINE).connect() as conn:
        params |= pick_warehouse_parameters(conn)
        index_sizes = {
            q.index: conn.execute(
                text("SELECT pg_relation_size(CAST(:i AS regclass))"),
                {"i": resolve_index(conn, q.index)},
            ).scalar_one()
            for q in queries(params)
            if q.index.startswith("warehouse.")
        }

    results = []
    for q in queries(params):
        with get_engine(q.role).connect() as conn:
            with_index = measure(conn, q.sql, args.runs)
            conn.rollback()
            trans = conn.begin()
            conn.execute(text(f"DROP INDEX {resolve_index(conn, q.index)}"))
            without_index = measure(conn, q.sql, args.runs)
            trans.rollback()  # index restored; nothing persisted
        results.append((q, with_index, without_index))
        print(
            f"{q.key}: with {with_index['median_ms']:.2f} ms, "
            f"without {without_index['median_ms']:.2f} ms"
        )

    lines = [
        "# Query plans: index decisions measured with EXPLAIN ANALYZE",
        "",
        f"Generated by `scripts/explain_queries.py` on {datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}. "
        "Do not edit by hand.",
        "",
        f"PostgreSQL {version}; src row counts: "
        + ", ".join(f"{t} {n:,}" for t, n in counts.items())
        + f". Median of {args.runs} warm runs. 'Without index' = index dropped inside a "
        "transaction that is rolled back. Timings are from a local Docker Desktop host and are "
        "only comparable with each other.",
        "",
        "| Query | Index under test | With index (ms) | Without (ms) | Ratio "
        "| Access with index | Access without |",
        "|---|---|--:|--:|--:|---|---|",
    ]
    for q, w, wo in results:
        lines.append(
            f"| {q.key} | `{q.index}` | {w['median_ms']:.2f} | {wo['median_ms']:.2f} | "
            f"{wo['median_ms'] / w['median_ms']:.1f}x | {'; '.join(w['access'])} | "
            f"{'; '.join(wo['access'])} |"
        )
    lines += [
        "",
        "Warehouse index sizes (built on every rebuild, then swapped in with the tables): "
        + ", ".join(f"`{i}` {n / 1024:,.0f} kB" for i, n in index_sizes.items())
        + ".",
    ]
    for q, w, wo in results:
        lines += [
            "",
            f"## {q.key}",
            "",
            q.question,
            "",
            "```sql",
            q.sql,
            "```",
            "",
            f"**With index** — median {w['median_ms']:.2f} ms, {w['buffers']:,} buffers",
            "",
            "```",
            w["plan"],
            "```",
            "",
            f"**Without index** — median {wo['median_ms']:.2f} ms, {wo['buffers']:,} buffers",
            "",
            "```",
            wo["plan"],
            "```",
        ]
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
