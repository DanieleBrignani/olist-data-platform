"""File-level schema validation of a source file against its contract.

Classification (ADR-0004):

BREAKING -> CRITICAL, ingestion of the file stops (DataContractError)
  * contract column missing from the header (includes renames)
  * duplicate column names in the header
  * empty file / no header
  * encoding different from the contract (e.g. BOM appeared/disappeared, invalid UTF-8)

NON-BREAKING -> WARNING, logged, ingestion continues
  * extra columns not in the contract (ignored by the loader)
  * column order differs from the contract (the loader maps by name)
  * line terminator differs from the contract (the CSV reader handles both)

Type changes cannot be seen in a header; they surface as record-level cast failures and
are bounded by the contract's max_reject_ratio.
"""

from __future__ import annotations

import codecs
import csv
from dataclasses import dataclass, field
from pathlib import Path

from olist_platform.errors import DataContractError
from olist_platform.utils.logging import get_logger
from olist_platform.validation.contracts import Contract

SNIFF_BYTES = 64 * 1024
CRLF = bytes([13, 10])

log = get_logger(__name__)


@dataclass(frozen=True)
class SchemaChange:
    kind: str
    detail: str


@dataclass
class SchemaCheckResult:
    table: str
    file: str
    header: list[str]
    breaking: list[SchemaChange] = field(default_factory=list)
    non_breaking: list[SchemaChange] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.breaking


def sniff_format(path: Path) -> tuple[bool, str]:
    """Return (starts with UTF-8 BOM, 'CRLF' | 'LF') from the head of the file."""
    with path.open("rb") as fh:
        head = fh.read(SNIFF_BYTES)
    return head.startswith(codecs.BOM_UTF8), "CRLF" if CRLF in head else "LF"


def read_header(path: Path, contract: Contract) -> list[str]:
    with path.open(encoding=contract.format.encoding, newline="") as fh:
        reader = csv.reader(
            fh, delimiter=contract.format.delimiter, quotechar=contract.format.quotechar
        )
        return next(reader, [])


def compare_header(contract: Contract, header: list[str]) -> SchemaCheckResult:
    """Pure function: classify differences between a header and the contract."""
    result = SchemaCheckResult(contract.table, contract.file, header)
    expected = contract.column_names

    if not header:
        result.breaking.append(SchemaChange("empty_header", "file has no header row"))
        return result

    duplicates = sorted({c for c in header if header.count(c) > 1})
    if duplicates:
        result.breaking.append(SchemaChange("duplicate_columns", f"duplicated: {duplicates}"))

    missing = [c for c in expected if c not in header]
    extra = [c for c in header if c not in expected]
    if missing:
        detail = f"missing: {missing}"
        if extra:
            detail += f" (unexpected present: {extra}; possible rename)"
        result.breaking.append(SchemaChange("missing_columns", detail))
    if extra:
        result.non_breaking.append(SchemaChange("extra_columns", f"ignored: {extra}"))

    if not missing and not duplicates:
        observed_order = [c for c in header if c in expected]
        if observed_order != expected:
            result.non_breaking.append(
                SchemaChange("column_order_changed", f"observed order: {observed_order}")
            )
    return result


def check_file(path: Path, contract: Contract) -> SchemaCheckResult:
    """Validate encoding, format and header of one file. Does not raise; see `enforce`."""
    has_bom, terminator = sniff_format(path)
    wants_bom = contract.format.encoding == "utf-8-sig"
    try:
        header = read_header(path, contract)
    except UnicodeDecodeError as exc:
        result = SchemaCheckResult(contract.table, contract.file, [])
        result.breaking.append(
            SchemaChange("encoding", f"not valid {contract.format.encoding}: {exc}")
        )
        return result

    result = compare_header(contract, header)
    if has_bom != wants_bom:
        # A BOM that appears unexpectedly corrupts the first column name; one that disappears
        # means the file was re-exported. Either way a human must look (and the header check
        # above has usually already failed for the unexpected-BOM case).
        result.breaking.append(
            SchemaChange("encoding", f"BOM present={has_bom}, contract expects {wants_bom}")
        )
    if terminator != contract.format.line_terminator:
        result.non_breaking.append(
            SchemaChange(
                "line_terminator",
                f"observed {terminator}, contract {contract.format.line_terminator}",
            )
        )
    return result


def enforce(result: SchemaCheckResult) -> None:
    """Log every change; raise DataContractError on any breaking change."""
    for change in result.non_breaking:
        log.warning(
            "schema_change_non_breaking",
            source=result.file,
            table=result.table,
            change=change.kind,
            detail=change.detail,
            severity="WARNING",
        )
    for change in result.breaking:
        log.error(
            "schema_change_breaking",
            source=result.file,
            table=result.table,
            change=change.kind,
            detail=change.detail,
            severity="CRITICAL",
        )
    if result.breaking:
        kinds = ", ".join(sorted({c.kind for c in result.breaking}))
        raise DataContractError(f"{result.file}: breaking schema change ({kinds})")
