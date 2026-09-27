"""Small, internally consistent SYNTHETIC Olist-shaped dataset for controlled tests.

Only for unit/integration/failure tests (allowed by the project rules); never used for
benchmarks or reported results. Files are written in each contract's real format
(encoding/BOM, line terminator), so they exercise the same code paths as the real data.
"""

from __future__ import annotations

import codecs
import csv
import io
from pathlib import Path

from olist_platform.validation.contracts import Contract

Rows = dict[str, list[dict[str, str]]]


def hexid(kind: int, n: int) -> str:
    return f"{kind:02x}{n:030x}"


def cust(n: int) -> str:
    return hexid(1, n)


def order(n: int) -> str:
    return hexid(2, n)


def prod(n: int) -> str:
    return hexid(3, n)


def seller(n: int) -> str:
    return hexid(4, n)


def review(n: int) -> str:
    return hexid(5, n)


def base_rows() -> Rows:
    """3 customers/orders, 2 products, 2 sellers; every relationship satisfied."""
    return {
        "geolocation": [
            {
                "geolocation_zip_code_prefix": f"0100{i}",
                "geolocation_lat": "-23.55",
                "geolocation_lng": "-46.63",
                "geolocation_city": "sao paulo",
                "geolocation_state": "SP",
            }
            for i in (1, 2, 3, 4)
        ],
        "product_category_translation": [
            {
                "product_category_name": "beleza_saude",
                "product_category_name_english": "health_beauty",
            },
        ],
        "customers": [
            {
                "customer_id": cust(i),
                "customer_unique_id": hexid(9, i),
                "customer_zip_code_prefix": f"0100{i}",
                "customer_city": "sao paulo",
                "customer_state": "SP",
            }
            for i in (1, 2, 3)
        ],
        "sellers": [
            {
                "seller_id": seller(i),
                "seller_zip_code_prefix": "01004",
                "seller_city": "campinas",
                "seller_state": "SP",
            }
            for i in (1, 2)
        ],
        "products": [
            {
                "product_id": prod(i),
                "product_category_name": "beleza_saude",
                "product_name_lenght": "40",
                "product_description_lenght": "300",
                "product_photos_qty": "2",
                "product_weight_g": "500",
                "product_length_cm": "20",
                "product_height_cm": "10",
                "product_width_cm": "15",
            }
            for i in (1, 2)
        ],
        "orders": [
            {
                "order_id": order(i),
                "customer_id": cust(i),
                "order_status": "delivered",
                "order_purchase_timestamp": f"2018-01-0{i} 10:00:00",
                "order_approved_at": f"2018-01-0{i} 11:00:00",
                "order_delivered_carrier_date": f"2018-01-0{i + 1} 09:00:00",
                "order_delivered_customer_date": f"2018-01-0{i + 4} 18:00:00",
                "order_estimated_delivery_date": "2018-01-20 00:00:00",
            }
            for i in (1, 2, 3)
        ],
        "order_items": [
            {
                "order_id": order(o),
                "order_item_id": "1",
                "product_id": prod(p),
                "seller_id": seller(s),
                "shipping_limit_date": "2018-01-10 00:00:00",
                "price": "59.90",
                "freight_value": "12.50",
            }
            for o, p, s in ((1, 1, 1), (2, 2, 2), (3, 1, 2))
        ],
        "order_payments": [
            {
                "order_id": order(i),
                "payment_sequential": "1",
                "payment_type": "credit_card",
                "payment_installments": "3",
                "payment_value": "72.40",
            }
            for i in (1, 2, 3)
        ],
        "order_reviews": [
            {
                "review_id": review(i),
                "order_id": order(i),
                "review_score": "5",
                "review_comment_title": "",
                "review_comment_message": "otimo\r\nproduto",
                "review_creation_date": "2018-01-10 00:00:00",
                "review_answer_timestamp": "2018-01-11 08:00:00",
            }
            for i in (1, 2, 3)
        ],
    }


def write_dataset(directory: Path, rows: Rows, contracts: dict[str, Contract]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for table, contract in contracts.items():
        buffer = io.StringIO()
        eol = "\r\n" if contract.format.line_terminator == "CRLF" else "\n"
        writer = csv.writer(buffer, lineterminator=eol)
        writer.writerow(contract.column_names)
        for row in rows.get(table, []):
            writer.writerow([row[c] for c in contract.column_names])
        data = buffer.getvalue().encode("utf-8")
        if contract.format.encoding == "utf-8-sig":
            data = codecs.BOM_UTF8 + data
        (directory / contract.file).write_bytes(data)
    return directory
