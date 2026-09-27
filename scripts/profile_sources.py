"""Profile the raw Olist files and write docs/source_profile.md.

Exploration tooling (not part of the pipeline): the facts it measures are the evidence behind
the choices in data_contracts/*.yml (types, nullability, uniqueness, FK severities).

Usage: python scripts/profile_sources.py [--data-dir DIR] [--out FILE]
"""

from __future__ import annotations

import argparse
import codecs
import csv
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

INT_RE = re.compile(r"^-?\d+$")
DEC_RE = re.compile(r"^-?\d+\.\d+$")
CRLF = bytes([13, 10])
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

# Candidate keys and relationships to measure (verified, not assumed, by this script)
CANDIDATE_KEYS = {
    "olist_customers_dataset.csv": [["customer_id"], ["customer_unique_id"]],
    "olist_orders_dataset.csv": [["order_id"]],
    "olist_order_items_dataset.csv": [["order_id", "order_item_id"]],
    "olist_order_payments_dataset.csv": [["order_id", "payment_sequential"]],
    "olist_order_reviews_dataset.csv": [["review_id"], ["review_id", "order_id"], ["order_id"]],
    "olist_products_dataset.csv": [["product_id"]],
    "olist_sellers_dataset.csv": [["seller_id"]],
    "olist_geolocation_dataset.csv": [
        ["geolocation_zip_code_prefix"],
        [
            "geolocation_zip_code_prefix",
            "geolocation_lat",
            "geolocation_lng",
            "geolocation_city",
            "geolocation_state",
        ],
    ],
    "product_category_name_translation.csv": [["product_category_name"]],
}
RELATIONSHIPS = [
    ("olist_orders_dataset.csv", "customer_id", "olist_customers_dataset.csv", "customer_id"),
    ("olist_order_items_dataset.csv", "order_id", "olist_orders_dataset.csv", "order_id"),
    ("olist_order_items_dataset.csv", "product_id", "olist_products_dataset.csv", "product_id"),
    ("olist_order_items_dataset.csv", "seller_id", "olist_sellers_dataset.csv", "seller_id"),
    ("olist_order_payments_dataset.csv", "order_id", "olist_orders_dataset.csv", "order_id"),
    ("olist_order_reviews_dataset.csv", "order_id", "olist_orders_dataset.csv", "order_id"),
    (
        "olist_products_dataset.csv",
        "product_category_name",
        "product_category_name_translation.csv",
        "product_category_name",
    ),
    (
        "olist_customers_dataset.csv",
        "customer_zip_code_prefix",
        "olist_geolocation_dataset.csv",
        "geolocation_zip_code_prefix",
    ),
    (
        "olist_sellers_dataset.csv",
        "seller_zip_code_prefix",
        "olist_geolocation_dataset.csv",
        "geolocation_zip_code_prefix",
    ),
]


@dataclass
class ColumnProfile:
    name: str
    empty: int = 0
    kinds: Counter = field(default_factory=Counter)
    values: set = field(default_factory=set)
    max_len: int = 0
    min_val: str | None = None
    max_val: str | None = None

    def observe(self, value: str) -> None:
        if value == "":
            self.empty += 1
            return
        self.values.add(value)
        self.max_len = max(self.max_len, len(value))
        if INT_RE.match(value):
            kind = "int"
        elif DEC_RE.match(value):
            kind = "decimal"
        elif TS_RE.match(value):
            kind = "timestamp"
        else:
            kind = "text"
        self.kinds[kind] += 1
        if kind != "text":
            key = float(value) if kind in ("int", "decimal") else value
            if self.min_val is None or key < self._key(self.min_val, kind):
                self.min_val = value
            if self.max_val is None or key > self._key(self.max_val, kind):
                self.max_val = value

    @staticmethod
    def _key(value: str, kind: str) -> float | str:
        return float(value) if kind in ("int", "decimal") else value


@dataclass
class FileProfile:
    name: str
    header: list[str]
    rows: int
    physical_lines: int
    bad_width_rows: int
    exact_duplicate_rows: int
    columns: list[ColumnProfile]
    key_duplicates: dict[str, int]
    bom: bool
    terminator: str


def sniff(path: Path) -> tuple[bool, str]:
    """(has UTF-8 BOM, line terminator) from the first 64 KiB."""
    with path.open("rb") as fh:
        head = fh.read(65536)
    return head.startswith(codecs.BOM_UTF8), "CRLF" if CRLF in head else "LF"


def profile_file(path: Path) -> tuple[FileProfile, list[list[str]]]:
    bom, terminator = sniff(path)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        physical_lines = sum(1 for _ in fh)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        cols = [ColumnProfile(h) for h in header]
        rows: list[list[str]] = []
        bad = 0
        for record in reader:
            if len(record) != len(header):
                bad += 1
                continue
            rows.append(record)
            for col, value in zip(cols, record, strict=True):
                col.observe(value)
    exact_dupes = len(rows) - len({tuple(r) for r in rows})
    key_dupes = {}
    for key in CANDIDATE_KEYS.get(path.name, []):
        idx = [header.index(k) for k in key]
        counts = Counter(tuple(r[i] for i in idx) for r in rows)
        key_dupes[" + ".join(key)] = sum(c - 1 for c in counts.values() if c > 1)
    return (
        FileProfile(
            path.name,
            header,
            len(rows) + bad,
            physical_lines,
            bad,
            exact_dupes,
            cols,
            key_dupes,
            bom,
            terminator,
        ),
        rows,
    )


def dominant_kind(col: ColumnProfile) -> str:
    if not col.kinds:
        return "empty"
    kinds = set(col.kinds)
    if kinds == {"int"}:
        return "int"
    if kinds <= {"int", "decimal"}:
        return "decimal"
    if kinds == {"timestamp"}:
        return "timestamp"
    return "text"


def render(profiles: list[FileProfile], fk_rows: list[tuple], data_dir: Path) -> str:
    out = [
        "# Source profile — Olist v2",
        "",
        f"Generated by `scripts/profile_sources.py` on {datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ} "
        f"from `{data_dir.as_posix()}`. Do not edit by hand.",
        "",
        "Row counts are CSV *records* (quoted multi-line fields count once); "
        "`physical lines` includes the header.",
        "",
        "## Files",
        "",
        "| File | Records | Physical lines | Bad-width records | Exact duplicates | BOM | EOL |",
        "|------|--------:|---------------:|------------------:|------------------------:|-----|-----|",
    ]
    out += [
        f"| {p.name} | {p.rows:,} | {p.physical_lines:,} | {p.bad_width_rows:,} | "
        f"{p.exact_duplicate_rows:,} | {'yes' if p.bom else 'no'} | {p.terminator} |"
        for p in profiles
    ]
    out += ["", "## Candidate keys (duplicate count = records beyond the first per key)", ""]
    out += ["| File | Key | Duplicates |", "|------|-----|-----------:|"]
    for p in profiles:
        out += [f"| {p.name} | `{k}` | {v:,} |" for k, v in p.key_duplicates.items()]
    out += ["", "## Relationships (child values with no parent)", ""]
    out += [
        "| Child | Parent | Distinct child values | Orphan distinct values | Orphan records |",
        "|-------|--------|---------------------:|----------------------:|---------------:|",
    ]
    out += [
        f"| {c}.{cc} | {p}.{pc} | {d:,} | {od:,} | {orr:,} |"
        for c, cc, p, pc, d, od, orr in fk_rows
    ]
    for p in profiles:
        out += ["", f"## {p.name}", ""]
        out += [
            "| Column | Inferred type | Empty | Distinct | Max len | Min | Max |",
            "|--------|---------------|------:|---------:|--------:|-----|-----|",
        ]
        out += [
            f"| {c.name} | {dominant_kind(c)} | {c.empty:,} | {len(c.values):,} | {c.max_len} | "
            f"{c.min_val or ''} | {c.max_val or ''} |"
            for c in p.columns
        ]
    return "\n".join(out) + "\n"


REVIEW_MIN, REVIEW_MAX = 1, 5
HEX32 = re.compile(r"^[0-9a-f]{32}$")
ZIP5 = re.compile(r"^[0-9]{5}$")
# Approximate bounding box of Brazil's territory (incl. oceanic islands), degrees
BR_LAT, BR_LNG = (-34.0, 5.5), (-74.0, -28.0)


def _in_box(r: dict[str, str]) -> bool:
    lat, lng = float(r["geolocation_lat"]), float(r["geolocation_lng"])
    return BR_LAT[0] <= lat <= BR_LAT[1] and BR_LNG[0] <= lng <= BR_LNG[1]


def _before(a: str, b: str) -> bool:
    """a < b for ISO-formatted timestamps; empty values never violate."""
    return bool(a) and bool(b) and a < b


# (file, description, predicate that returns True when the record VIOLATES the expectation)
CONSISTENCY_CHECKS = [
    (
        "olist_orders_dataset.csv",
        "order_id not 32-char lowercase hex",
        lambda r: not HEX32.match(r["order_id"]),
    ),
    (
        "olist_orders_dataset.csv",
        "customer_id not 32-char lowercase hex",
        lambda r: not HEX32.match(r["customer_id"]),
    ),
    (
        "olist_orders_dataset.csv",
        "approved before purchase",
        lambda r: _before(r["order_approved_at"], r["order_purchase_timestamp"]),
    ),
    (
        "olist_orders_dataset.csv",
        "handed to carrier before purchase",
        lambda r: _before(r["order_delivered_carrier_date"], r["order_purchase_timestamp"]),
    ),
    (
        "olist_orders_dataset.csv",
        "delivered to customer before purchase",
        lambda r: _before(r["order_delivered_customer_date"], r["order_purchase_timestamp"]),
    ),
    (
        "olist_orders_dataset.csv",
        "delivered to customer before handed to carrier",
        lambda r: _before(r["order_delivered_customer_date"], r["order_delivered_carrier_date"]),
    ),
    (
        "olist_orders_dataset.csv",
        "status 'delivered' without customer delivery date",
        lambda r: r["order_status"] == "delivered" and not r["order_delivered_customer_date"],
    ),
    (
        "olist_orders_dataset.csv",
        "status 'canceled' with customer delivery date",
        lambda r: r["order_status"] == "canceled" and bool(r["order_delivered_customer_date"]),
    ),
    ("olist_order_items_dataset.csv", "price <= 0", lambda r: float(r["price"]) <= 0),
    ("olist_order_items_dataset.csv", "freight_value < 0", lambda r: float(r["freight_value"]) < 0),
    (
        "olist_order_items_dataset.csv",
        "product_id / seller_id not 32-char hex",
        lambda r: not (HEX32.match(r["product_id"]) and HEX32.match(r["seller_id"])),
    ),
    (
        "olist_order_payments_dataset.csv",
        "payment_value < 0",
        lambda r: float(r["payment_value"]) < 0,
    ),
    (
        "olist_order_payments_dataset.csv",
        "payment_value = 0",
        lambda r: float(r["payment_value"]) == 0,
    ),
    (
        "olist_order_payments_dataset.csv",
        "payment_installments < 1",
        lambda r: int(r["payment_installments"]) < 1,
    ),
    (
        "olist_order_payments_dataset.csv",
        "payment_type = 'not_defined'",
        lambda r: r["payment_type"] == "not_defined",
    ),
    (
        "olist_order_reviews_dataset.csv",
        "review_score outside 1..5",
        lambda r: not REVIEW_MIN <= int(r["review_score"]) <= REVIEW_MAX,
    ),
    (
        "olist_order_reviews_dataset.csv",
        "answered before created",
        lambda r: _before(r["review_answer_timestamp"], r["review_creation_date"]),
    ),
    (
        "olist_order_reviews_dataset.csv",
        "review_id not 32-char hex",
        lambda r: not HEX32.match(r["review_id"]),
    ),
    ("olist_products_dataset.csv", "product_weight_g = 0", lambda r: r["product_weight_g"] == "0"),
    (
        "olist_customers_dataset.csv",
        "zip prefix not 5 digits",
        lambda r: not ZIP5.match(r["customer_zip_code_prefix"]),
    ),
    (
        "olist_sellers_dataset.csv",
        "zip prefix not 5 digits",
        lambda r: not ZIP5.match(r["seller_zip_code_prefix"]),
    ),
    (
        "olist_sellers_dataset.csv",
        "seller_city contains digits",
        lambda r: any(ch.isdigit() for ch in r["seller_city"]),
    ),
    (
        "olist_geolocation_dataset.csv",
        "zip prefix not 5 digits",
        lambda r: not ZIP5.match(r["geolocation_zip_code_prefix"]),
    ),
    (
        "olist_geolocation_dataset.csv",
        "coordinates outside Brazil bounding box",
        lambda r: not _in_box(r),
    ),
]
ENUM_MAX_DISTINCT = 30


def consistency_section(all_rows: dict[str, tuple[list[str], list[list[str]]]]) -> list[str]:
    out = ["", "## Record-level consistency observations", ""]
    out += [
        "| File | Observation | Violating records |",
        "|------|-------------|------------------:|",
    ]
    for name, description, violates in CONSISTENCY_CHECKS:
        header, rows = all_rows[name]
        count = sum(1 for r in rows if violates(dict(zip(header, r, strict=True))))
        out.append(f"| {name} | {description} | {count:,} |")
    return out


def enum_section(profiles: list[FileProfile]) -> list[str]:
    out = ["", f"## Low-cardinality text columns (≤ {ENUM_MAX_DISTINCT} distinct values)", ""]
    for p in profiles:
        for c in p.columns:
            if dominant_kind(c) == "text" and len(c.values) <= ENUM_MAX_DISTINCT:
                out.append(f"* `{p.name}.{c.name}`: {', '.join(sorted(c.values))}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw/olist/v2"))
    parser.add_argument("--out", type=Path, default=Path("docs/source_profile.md"))
    args = parser.parse_args()

    profiles, all_rows = [], {}
    for path in sorted(args.data_dir.glob("*.csv")):
        prof, rows = profile_file(path)
        profiles.append(prof)
        all_rows[path.name] = (prof.header, rows)

    fk_rows = []
    for child, ccol, parent, pcol in RELATIONSHIPS:
        ch, crows = all_rows[child]
        ph, prows = all_rows[parent]
        ci, pi = ch.index(ccol), ph.index(pcol)
        parents = {r[pi] for r in prows}
        child_vals = [r[ci] for r in crows if r[ci] != ""]
        orphans = [v for v in child_vals if v not in parents]
        fk_rows.append(
            (child, ccol, parent, pcol, len(set(child_vals)), len(set(orphans)), len(orphans))
        )

    report = render(profiles, fk_rows, args.data_dir)
    report += "\n".join(consistency_section(all_rows) + enum_section(profiles)) + "\n"
    args.out.write_text(report, encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
