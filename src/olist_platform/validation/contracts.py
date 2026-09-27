"""Source data contracts: typed model + loader for data_contracts/*.yml.

A contract states what the platform promises about one source file:
columns (type, nullability, uniqueness, meaning), primary key, foreign keys and
record-level rules, each with a severity (ADR-0004). The model validates the contract
itself; `load_contracts` additionally validates cross-contract references.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTRACTS_DIR = Path(__file__).resolve().parents[3] / "data_contracts"
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


class Severity(StrEnum):
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class ColumnType(StrEnum):
    TEXT = "text"
    INTEGER = "integer"
    NUMERIC = "numeric"
    TIMESTAMP = "timestamp"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FileFormat(_Strict):
    encoding: Literal["utf-8", "utf-8-sig"] = "utf-8"
    delimiter: str = ","
    quotechar: str = '"'
    line_terminator: Literal["LF", "CRLF"] = "LF"


class Column(_Strict):
    name: str
    type: ColumnType
    nullable: bool
    unique: bool = False
    description: str = Field(min_length=10)
    max_length: int | None = None
    pattern: str | None = None
    target_name: str | None = None  # rename in src when the source name is wrong/misspelled

    @field_validator("name")
    @classmethod
    def _identifier(cls, v: str) -> str:
        if not IDENTIFIER.match(v):
            raise ValueError(f"column name {v!r} is not a lowercase identifier")
        return v

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v: str | None) -> str | None:
        if v is not None:
            re.compile(v)
        return v


class Reference(_Strict):
    table: str
    columns: list[str]


class ForeignKey(_Strict):
    columns: list[str]
    references: Reference
    severity: Severity
    description: str = Field(min_length=10)


class _RuleBase(_Strict):
    name: str
    severity: Severity
    description: str = Field(min_length=10)


class AcceptedValuesRule(_RuleBase):
    check: Literal["accepted_values"]
    column: str
    values: list[str] = Field(min_length=1)


class RangeRule(_RuleBase):
    check: Literal["range"]
    column: str
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _bounded(self) -> RangeRule:
        if self.min is None and self.max is None:
            raise ValueError(f"range rule {self.name!r} needs min and/or max")
        return self


class ExpressionRule(_RuleBase):
    """SQL boolean over *typed* columns of one record; NULL result counts as pass."""

    check: Literal["expression"]
    columns: list[str] = Field(min_length=1)
    sql: str


Rule = Annotated[AcceptedValuesRule | RangeRule | ExpressionRule, Field(discriminator="check")]


class Contract(_Strict):
    contract_version: int
    table: str
    file: str
    dataset_version: int
    description: str = Field(min_length=20)
    grain: str
    owner: str
    format: FileFormat = FileFormat()
    primary_key: list[str]
    deduplicate_exact_rows: bool = False
    max_reject_ratio: float = Field(ge=0, le=1)
    columns: list[Column] = Field(min_length=1)
    foreign_keys: list[ForeignKey] = []
    rules: list[Rule] = []

    @field_validator("table")
    @classmethod
    def _table_identifier(cls, v: str) -> str:
        # Table names are interpolated into DDL/DML (raw.<table>); only plain identifiers allowed
        if not IDENTIFIER.match(v):
            raise ValueError(f"table name {v!r} is not a lowercase identifier")
        return v

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def column(self, name: str) -> Column:
        return next(c for c in self.columns if c.name == name)

    @model_validator(mode="after")
    def _internal_consistency(self) -> Contract:
        names = self.column_names
        if len(names) != len(set(names)):
            raise ValueError(f"{self.table}: duplicate column names")
        known = set(names)
        refs = {"primary_key": self.primary_key}
        refs |= {f"foreign_key {fk.columns}": fk.columns for fk in self.foreign_keys}
        for rule in self.rules:
            cols = rule.columns if isinstance(rule, ExpressionRule) else [rule.column]
            refs[f"rule {rule.name}"] = cols
        for where, cols in refs.items():
            unknown = set(cols) - known
            if unknown:
                raise ValueError(f"{self.table}: {where} references unknown columns {unknown}")
        for pk_col in self.primary_key:
            if self.column(pk_col).nullable:
                raise ValueError(f"{self.table}: primary key column {pk_col} must be non-nullable")
        rule_names = [r.name for r in self.rules]
        if len(rule_names) != len(set(rule_names)):
            raise ValueError(f"{self.table}: duplicate rule names")
        return self


def load_contract(path: Path) -> Contract:
    with path.open(encoding="utf-8") as fh:
        return Contract.model_validate(yaml.safe_load(fh))


def validate_references(contracts: dict[str, Contract]) -> None:
    """Every FK must target an existing contract's columns, with matching arity and type."""
    for contract in contracts.values():
        for fk in contract.foreign_keys:
            target = contracts.get(fk.references.table)
            if target is None:
                raise ValueError(
                    f"{contract.table}: foreign key targets unknown table {fk.references.table}"
                )
            if len(fk.columns) != len(fk.references.columns):
                raise ValueError(f"{contract.table}: foreign key arity mismatch {fk.columns}")
            for child_col, parent_col in zip(fk.columns, fk.references.columns, strict=True):
                if parent_col not in target.column_names:
                    raise ValueError(f"{contract.table}: {parent_col} not in {target.table}")
                child_t, parent_t = contract.column(child_col).type, target.column(parent_col).type
                if child_t != parent_t:
                    raise ValueError(
                        f"{contract.table}.{child_col} ({child_t}) -> "
                        f"{target.table}.{parent_col} ({parent_t}): type mismatch"
                    )


def load_contracts(directory: Path = CONTRACTS_DIR) -> dict[str, Contract]:
    paths = sorted(directory.glob("*.yml"))
    if not paths:
        raise FileNotFoundError(f"no contracts found in {directory}")
    contracts: dict[str, Contract] = {}
    for path in paths:
        contract = load_contract(path)
        if contract.table in contracts:
            raise ValueError(f"duplicate contract for table {contract.table}")
        contracts[contract.table] = contract
    files = [c.file for c in contracts.values()]
    if len(files) != len(set(files)):
        raise ValueError("two contracts claim the same source file")
    validate_references(contracts)
    return contracts
