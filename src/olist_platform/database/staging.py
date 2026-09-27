"""load_staging: raw.<table> (text) -> src.<table> (typed, contract-conforming).

Everything below runs in ONE transaction, set-based, per table in foreign-key order:

 1. text checks on the raw record      not_null / type (pg_input_is_valid) / pattern / max_length
 2. typed stage                        castable records, '' -> NULL for every column
 3. contract rules                     accepted_values / range / expression, on typed values
 4. exact duplicates                   flagged WARNING unless the contract expects them
 5. primary-key conflicts              same key, different content -> ALL such records quarantined
 6. foreign keys                       checked against the parent's *valid* rows (cascades)
 7. collapse exact duplicates          one src row, source_record_count = copies

ERROR -> record quarantined (not in src); WARNING -> record flagged (kept). Both are written to
meta.rejected_records (layer='src'). If any table's quarantined share exceeds its contract's
max_reject_ratio, src is left untouched, the evidence is still committed, and
DataContractError is raised (CRITICAL). Otherwise all src tables are replaced atomically.

Reconciliation invariant per table (asserted in tests):
    raw records = sum(src.source_record_count) + distinct quarantined records
"""

from __future__ import annotations

import time
import uuid
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import Connection, Engine, text

from olist_platform.errors import DataContractError
from olist_platform.ingestion.runs import record_event
from olist_platform.utils.logging import get_logger
from olist_platform.validation.contracts import (
    AcceptedValuesRule,
    Column,
    ColumnType,
    Contract,
    ExpressionRule,
    RangeRule,
    Severity,
)

TASK = "load_staging"
PG_TYPE = {
    ColumnType.TEXT: "text",
    ColumnType.INTEGER: "integer",
    ColumnType.NUMERIC: "numeric",
    ColumnType.TIMESTAMP: "timestamp",
}

log = get_logger(__name__)


def lit(value: str) -> str:
    """SQL string literal (standard_conforming_strings=on). Only used for contract content."""
    return "'" + value.replace("'", "''") + "'"


def action_for(severity: Severity) -> str:
    return "flagged" if severity == Severity.WARNING else "quarantined"


@dataclass(frozen=True)
class Check:
    rule_name: str
    severity: Severity
    violated_sql: str
    reason_sql: str


@dataclass
class TableResult:
    table: str
    source_file_id: int
    raw_rows: int = 0
    src_rows: int = 0
    quarantined: int = 0
    flagged: int = 0
    duplicates_collapsed: int = 0
    issues_by_rule: dict[str, int] = field(default_factory=dict)
    max_reject_ratio: float = 0.0

    @property
    def reject_ratio(self) -> float:
        return self.quarantined / self.raw_rows if self.raw_rows else 0.0


# ---------------------------------------------------------------- check compilation (pure)


def text_checks(contract: Contract) -> list[Check]:
    """Implicit column checks, evaluated on raw text (alias s)."""
    checks: list[Check] = []
    for col in contract.columns:
        v = f"s.{col.name}"
        if not col.nullable:
            checks.append(
                Check(
                    f"not_null__{col.name}",
                    Severity.ERROR,
                    f"({v} IS NULL OR {v} = '')",
                    lit(f"{col.name} is empty"),
                )
            )
        if col.type != ColumnType.TEXT:
            pg = PG_TYPE[col.type]
            checks.append(
                Check(
                    f"type__{col.name}",
                    Severity.ERROR,
                    f"({v} <> '' AND NOT pg_input_is_valid({v}, {lit(pg)}))",
                    f"{lit(f'{col.name} is not a valid {pg}: ')} || left({v}, 100)",
                )
            )
        if col.pattern:
            checks.append(
                Check(
                    f"pattern__{col.name}",
                    Severity.ERROR,
                    f"({v} <> '' AND {v} !~ {lit(col.pattern)})",
                    f"{lit(f'{col.name} does not match {col.pattern}: ')} || left({v}, 100)",
                )
            )
        if col.max_length:
            checks.append(
                Check(
                    f"max_length__{col.name}",
                    Severity.ERROR,
                    f"(length({v}) > {int(col.max_length)})",
                    f"{lit(f'{col.name} longer than {col.max_length}: ')} || length({v})",
                )
            )
    return checks


def _values(columns: list[str]) -> str:
    parts = [f"{lit(c + '=')} || coalesce(s.{c}::text, 'NULL')" for c in columns]
    return " || ', ' || ".join(parts)


def rule_checks(contract: Contract) -> list[Check]:
    """Contract rules, evaluated on the typed stage (alias s). NULL never violates a rule."""
    checks: list[Check] = []
    for rule in contract.rules:
        if isinstance(rule, AcceptedValuesRule):
            allowed = ", ".join(lit(v) for v in rule.values)
            violated = (
                f"(s.{rule.column} IS NOT NULL AND s.{rule.column} <> ALL (ARRAY[{allowed}]))"
            )
            reason = f"{_values([rule.column])} || {lit(' not in accepted values')}"
        elif isinstance(rule, RangeRule):
            bounds = []
            if rule.min is not None:
                bounds.append(f"s.{rule.column} < {rule.min!r}")
            if rule.max is not None:
                bounds.append(f"s.{rule.column} > {rule.max!r}")
            violated = f"({' OR '.join(bounds)})"
            reason = f"{_values([rule.column])} || {lit(f' outside [{rule.min}, {rule.max}]')}"
        elif isinstance(rule, ExpressionRule):
            # Rule SQL uses bare column names, which resolve to the stage row (alias s)
            violated = f"(({rule.sql}) IS FALSE)"
            reason = f"{lit(f'violates {rule.sql}: ')} || {_values(rule.columns)}"
        else:  # pragma: no cover - the discriminated union is exhaustive
            raise TypeError(rule)
        checks.append(Check(rule.name, rule.severity, violated, reason))
    return checks


def issues_insert_sql(table: str, checks: list[Check], relation: str, where: str = "TRUE") -> str:
    """One INSERT for all checks of a table.

    A cheap boolean prefilter (any check violated?) runs first, so the per-check LATERAL
    expansion and the reason strings are only computed for the few violating records.
    Without it every record produced len(checks) candidate rows (10M for geolocation).
    """
    rows = ",\n            ".join(
        f"({lit(c.rule_name)}, {lit(c.severity.value)}, {lit(action_for(c.severity))}, "
        f"{c.violated_sql}, {c.reason_sql})"
        for c in checks
    )
    any_violated = " OR ".join(f"COALESCE({c.violated_sql}, FALSE)" for c in checks)
    return f"""
        INSERT INTO _issues (source_table, source_file_id, row_number, rule_name, severity,
                             action, reason)
        SELECT {lit(table)}, s._source_file_id, s._row_number, v.rule, v.severity, v.action,
               v.reason
        FROM (SELECT * FROM {relation} AS s WHERE ({where}) AND ({any_violated})) AS s
        CROSS JOIN LATERAL (VALUES
            {rows}
        ) AS v(rule, severity, action, violated, reason)
        WHERE v.violated"""


def topological_order(contracts: dict[str, Contract]) -> list[Contract]:
    """Parents before children, following every declared foreign key (deterministic)."""
    deps = {t: {fk.references.table for fk in c.foreign_keys} for t, c in contracts.items()}
    ordered: list[Contract] = []
    done: set[str] = set()
    remaining = set(contracts)
    while remaining:
        ready = sorted(t for t in remaining if deps[t] <= done)
        if not ready:
            raise DataContractError(f"foreign-key cycle among {sorted(remaining)}")
        ordered.extend(contracts[t] for t in ready)
        done.update(ready)
        remaining.difference_update(ready)
    return ordered


def src_name(col: Column) -> str:
    return col.target_name or col.name


# ---------------------------------------------------------------- execution


def _stage_table(conn: Connection, contract: Contract, source_file_id: int) -> TableResult:
    t = contract.table
    stage, final = f"_stage_{t}", f"_final_{t}"
    cols = contract.column_names
    col_list = ", ".join(cols)
    result = TableResult(t, source_file_id, max_reject_ratio=contract.max_reject_ratio)
    params = {"fid": source_file_id, "t": t}

    result.raw_rows = conn.execute(
        text(f"SELECT count(*) FROM raw.{t} WHERE _source_file_id = :fid"),
        params,
    ).scalar_one()

    # 1. text-level checks on raw
    conn.execute(
        text(issues_insert_sql(t, text_checks(contract), f"raw.{t}", "s._source_file_id = :fid")),
        params,
    )

    # 2. typed stage of records without quarantined issues
    typed = ", ".join(
        f"NULLIF(r.{c.name}, '')"
        + ("" if c.type == ColumnType.TEXT else f"::{PG_TYPE[c.type]}")
        + f" AS {c.name}"
        for c in contract.columns
    )
    conn.execute(
        text(f"""
        CREATE TEMP TABLE {stage} ON COMMIT DROP AS
        SELECT r._source_file_id, r._row_number, {typed}
        FROM raw.{t} r
        WHERE r._source_file_id = :fid
          AND NOT EXISTS (SELECT 1 FROM _issues i WHERE i.source_table = :t
                          AND i.row_number = r._row_number AND i.action = 'quarantined')"""),
        params,
    )
    conn.execute(text(f"ANALYZE {stage}"))

    def drop_quarantined() -> None:
        conn.execute(
            text(f"""
            DELETE FROM {stage} s USING _issues i
            WHERE i.source_table = :t AND i.action = 'quarantined'
              AND i.row_number = s._row_number"""),
            params,
        )

    # 3. contract rules on typed values
    checks = rule_checks(contract)
    if checks:
        conn.execute(text(issues_insert_sql(t, checks, stage)), params)
        drop_quarantined()

    # 4. exact duplicates (flag extra copies unless the contract expects duplicates)
    if not contract.deduplicate_exact_rows:
        conn.execute(
            text(f"""
            INSERT INTO _issues (source_table, source_file_id, row_number, rule_name, severity,
                                 action, reason)
            SELECT :t, _source_file_id, _row_number, 'exact_duplicate', 'WARNING', 'flagged',
                   'identical to record ' || first_row
            FROM (SELECT _source_file_id, _row_number,
                         first_value(_row_number) OVER w AS first_row,
                         row_number() OVER w AS rn
                  FROM {stage} WINDOW w AS (PARTITION BY {col_list} ORDER BY _row_number)) d
            WHERE rn > 1"""),
            params,
        )

    # 5. primary-key conflicts: same key, different content -> quarantine every record
    if contract.primary_key:
        pk = ", ".join(contract.primary_key)
        conn.execute(
            text(f"""
            INSERT INTO _issues (source_table, source_file_id, row_number, rule_name, severity,
                                 action, reason)
            SELECT :t, s._source_file_id, s._row_number, 'primary_key_conflict', 'ERROR',
                   'quarantined', d.versions || ' different records share key ({pk})'
            FROM {stage} s
            JOIN (SELECT {pk}, count(DISTINCT ROW({col_list})::text) AS versions
                  FROM {stage} GROUP BY {pk}
                  HAVING count(DISTINCT ROW({col_list})::text) > 1) d USING ({pk})"""),
            params,
        )
        drop_quarantined()

    # 6. foreign keys against the parent's final (valid) rows
    for fk in contract.foreign_keys:
        parent = f"_final_{fk.references.table}"
        join = " AND ".join(
            f"p.{pc} = s.{cc}" for cc, pc in zip(fk.columns, fk.references.columns, strict=True)
        )
        not_null = " AND ".join(f"s.{c} IS NOT NULL" for c in fk.columns)
        rule = f"foreign_key__{'__'.join(fk.columns)}"
        reason = (
            f"{_values(fk.columns)} || "
            f"{lit(f' not found in {fk.references.table} (missing or quarantined)')}"
        )
        conn.execute(
            text(f"""
            INSERT INTO _issues (source_table, source_file_id, row_number, rule_name, severity,
                                 action, reason)
            SELECT :t, s._source_file_id, s._row_number, {lit(rule)},
                   {lit(fk.severity.value)}, {lit(action_for(fk.severity))}, {reason}
            FROM {stage} s
            WHERE {not_null} AND NOT EXISTS (SELECT 1 FROM {parent} p WHERE {join})"""),
            params,
        )
    drop_quarantined()

    # 7. collapse exact duplicates into final rows
    conn.execute(
        text(f"""
        CREATE TEMP TABLE {final} ON COMMIT DROP AS
        SELECT min(_source_file_id) AS _source_file_id, min(_row_number) AS _row_number,
               count(*)::integer AS source_record_count, {col_list}
        FROM {stage} GROUP BY {col_list}""")
    )
    conn.execute(text(f"ANALYZE {final}"))

    stats = conn.execute(
        text(f"""
        SELECT (SELECT count(*) FROM {final}),
               (SELECT count(*) FROM {stage}) - (SELECT count(*) FROM {final}),
               (SELECT count(DISTINCT row_number) FROM _issues
                 WHERE source_table = :t AND action = 'quarantined'),
               (SELECT count(DISTINCT row_number) FROM _issues f
                 WHERE f.source_table = :t AND f.action = 'flagged'
                   AND NOT EXISTS (SELECT 1 FROM _issues q WHERE q.source_table = :t
                                   AND q.row_number = f.row_number
                                   AND q.action = 'quarantined'))"""),
        params,
    ).one()
    result.src_rows, result.duplicates_collapsed, result.quarantined, result.flagged = stats
    result.issues_by_rule = dict(
        conn.execute(
            text("SELECT rule_name, count(*) FROM _issues WHERE source_table = :t GROUP BY 1"),
            params,
        ).all()
    )
    return result


def _publish_src(conn: Connection, ordered: list[Contract]) -> None:
    conn.execute(text("TRUNCATE " + ", ".join(f"src.{c.table}" for c in ordered)))
    for contract in ordered:
        targets = ", ".join(src_name(c) for c in contract.columns)
        sources = ", ".join(contract.column_names)
        conn.execute(
            text(f"""
            INSERT INTO src.{contract.table}
                ({targets}, _source_file_id, _row_number, source_record_count)
            SELECT {sources}, _source_file_id, _row_number, source_record_count
            FROM _final_{contract.table}""")
        )
        conn.execute(text(f"ANALYZE src.{contract.table}"))


def _persist_issues(conn: Connection, run_id: uuid.UUID, ordered: list[Contract]) -> None:
    for contract in ordered:
        conn.execute(
            text(f"""
            INSERT INTO meta.rejected_records (pipeline_run_id, source_file_id, source_table,
                record_ref, rule_name, severity, reason, raw_record, layer, action,
                source_row_number)
            SELECT :run, i.source_file_id, i.source_table,
                   sf.file_name || '#record=' || i.row_number, i.rule_name, i.severity,
                   i.reason, to_jsonb(r) - '_loaded_at' - '_pipeline_run_id', 'src', i.action,
                   i.row_number
            FROM _issues i
            JOIN meta.source_files sf ON sf.source_file_id = i.source_file_id
            LEFT JOIN raw.{contract.table} r
              ON r._source_file_id = i.source_file_id AND r._row_number = i.row_number
            WHERE i.source_table = :t"""),
            {"run": run_id, "t": contract.table},
        )


def current_files(conn: Connection, contracts: dict[str, Contract]) -> dict[str, int]:
    rows = dict(
        conn.execute(
            text("SELECT source_table, source_file_id FROM meta.current_source_files")
        ).all()
    )
    missing = sorted(set(contracts) - set(rows))
    if missing:
        raise DataContractError(f"no loaded raw file for {missing}; run ingestion first")
    return {t: rows[t] for t in contracts}


def load_staging(
    engine: Engine, run_id: uuid.UUID, contracts: dict[str, Contract]
) -> list[TableResult]:
    started = time.perf_counter()
    ordered = topological_order(contracts)
    results: list[TableResult] = []
    with engine.begin() as conn:
        files = current_files(conn, contracts)
        conn.execute(
            text("""
            CREATE TEMP TABLE _issues (
                source_table text, source_file_id bigint, row_number bigint, rule_name text,
                severity text, action text, reason text
            ) ON COMMIT DROP""")
        )
        conn.execute(text("CREATE INDEX ON _issues (source_table, row_number)"))

        for contract in ordered:
            table_started = time.perf_counter()
            result = _stage_table(conn, contract, files[contract.table])
            results.append(result)
            log.info(
                "table_staged",
                task=TASK,
                table=contract.table,
                rows_read=result.raw_rows,
                rows_written=result.src_rows,
                rows_rejected=result.quarantined,
                rows_flagged=result.flagged,
                duplicates_collapsed=result.duplicates_collapsed,
                duration_ms=round((time.perf_counter() - table_started) * 1000, 1),
            )

        over_limit = [r for r in results if r.reject_ratio > r.max_reject_ratio]
        _persist_issues(conn, run_id, ordered)
        if not over_limit:
            _publish_src(conn, ordered)
        for r in results:
            failed = r in over_limit
            record_event(
                conn,
                run_id,
                task=TASK,
                source=r.table,
                source_file_id=r.source_file_id,
                event="table_rejected" if failed else "table_staged",
                status="failed" if failed else "ok",
                rows_read=r.raw_rows,
                rows_written=0 if over_limit else r.src_rows,
                rows_rejected=r.quarantined,
                details={
                    "flagged": r.flagged,
                    "duplicates_collapsed": r.duplicates_collapsed,
                    "issues_by_rule": r.issues_by_rule,
                    "reject_ratio": round(r.reject_ratio, 6),
                    "max_reject_ratio": r.max_reject_ratio,
                    "src_published": not over_limit,
                },
            )
        # the transaction commits here: evidence always, src only if every table passed

    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    totals = Counter()
    for r in results:
        totals.update(
            {
                "read": r.raw_rows,
                "written": r.src_rows,
                "quarantined": r.quarantined,
                "flagged": r.flagged,
            }
        )
    if over_limit:
        detail = ", ".join(
            f"{r.table} {r.reject_ratio:.2%} > {r.max_reject_ratio:.2%}" for r in over_limit
        )
        log.error(
            "staging_rejected",
            task=TASK,
            status="failed",
            severity="CRITICAL",
            detail=detail,
            duration_ms=duration_ms,
        )
        raise DataContractError(f"reject ratio over contract limit: {detail}; src unchanged")
    log.info(
        "staging_published",
        task=TASK,
        status="ok",
        rows_read=totals["read"],
        rows_written=totals["written"],
        rows_rejected=totals["quarantined"],
        rows_flagged=totals["flagged"],
        duration_ms=duration_ms,
    )
    return results
