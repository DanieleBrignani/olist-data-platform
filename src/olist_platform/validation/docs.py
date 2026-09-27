"""Render data contracts as Markdown (docs/data_contracts.md) so docs never drift from YAML."""

from __future__ import annotations

from olist_platform.validation.contracts import (
    AcceptedValuesRule,
    Contract,
    ExpressionRule,
    RangeRule,
)


def _rule_text(rule: AcceptedValuesRule | RangeRule | ExpressionRule) -> str:
    if isinstance(rule, AcceptedValuesRule):
        return f"`{rule.column}` in {{{', '.join(rule.values)}}}"
    if isinstance(rule, RangeRule):
        lo = "" if rule.min is None else f"{rule.min:g} ≤ "
        hi = "" if rule.max is None else f" ≤ {rule.max:g}"
        return f"{lo}`{rule.column}`{hi}"
    return f"`{rule.sql}`"


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def render_contract(c: Contract) -> list[str]:
    out = [
        f"## `{c.table}` ← `{c.file}`",
        "",
        _cell(c.description),
        "",
        f"* **Grain:** {_cell(c.grain)}",
        f"* **Primary key:** {', '.join(f'`{k}`' for k in c.primary_key) or '— (none; see grain)'}",
        f"* **Format:** {c.format.encoding}, {c.format.line_terminator}, "
        f"delimiter `{c.format.delimiter}`",
        f"* **Max reject ratio before CRITICAL:** {c.max_reject_ratio:.2%}",
    ]
    if c.deduplicate_exact_rows:
        out.append("* **Exact duplicate records:** collapsed in `src`, logged as WARNING")
    out += [
        "",
        "| Column | Type | Nullable | Unique | Constraint | Meaning |",
        "|--------|------|----------|--------|------------|---------|",
    ]
    for col in c.columns:
        name = f"`{col.name}`" + (f" → `{col.target_name}`" if col.target_name else "")
        constraint = ", ".join(
            filter(
                None,
                [
                    col.pattern and f"pattern `{col.pattern}`",
                    col.max_length and f"max len {col.max_length}",
                ],
            )
        )
        out.append(
            f"| {name} | {col.type} | {'yes' if col.nullable else 'no'} | "
            f"{'yes' if col.unique else ''} | {constraint} | {_cell(col.description)} |"
        )
    if c.foreign_keys:
        out += ["", "| Foreign key | References | Severity | Why |", "|---|---|---|---|"]
        for fk in c.foreign_keys:
            out.append(
                f"| {', '.join(fk.columns)} | {fk.references.table}"
                f"({', '.join(fk.references.columns)}) | {fk.severity} | {_cell(fk.description)} |"
            )
    if c.rules:
        out += ["", "| Rule | Check | Severity | Why |", "|---|---|---|---|"]
        for rule in c.rules:
            out.append(
                f"| `{rule.name}` | {_rule_text(rule)} | {rule.severity} | "
                f"{_cell(rule.description)} |"
            )
    return [*out, ""]


def render(contracts: dict[str, Contract]) -> str:
    out = [
        "# Source data contracts",
        "",
        "Generated from `data_contracts/*.yml` by `olist contracts docs`. Do not edit by hand.",
        "",
        "Implicit rules for every contract: values must cast to the declared type, non-nullable",
        "columns must be present, `pattern`/`max length` must hold, primary keys must be unique.",
        "Violations are ERROR (record quarantined in `meta.rejected_records`). If the share of",
        "rejected records exceeds the contract's max reject ratio the file is CRITICAL.",
        "",
        "| Table | File | Grain | Columns | Rules |",
        "|-------|------|-------|--------:|------:|",
    ]
    out += [
        f"| `{c.table}` | `{c.file}` | {_cell(c.grain)} | {len(c.columns)} | {len(c.rules)} |"
        for c in contracts.values()
    ]
    out.append("")
    for c in contracts.values():
        out += render_contract(c)
    return "\n".join(out)
