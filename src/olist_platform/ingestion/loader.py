"""Idempotent file -> raw.<table> loader (ADR-0008).

Per file, in order:
 1. schema check against the contract (breaking change -> DataContractError, nothing written)
 2. idempotency: (table, sha256) already `loaded` -> recorded no-op unless force_reload
 3. ONE transaction: register/reuse meta.source_files row, delete previous rows of that file,
    stream records with COPY, quarantine structurally invalid records, reconcile counts,
    mark `loaded`, write an ingestion event
 4. if the reject ratio exceeds the contract limit, the data transaction is rolled back and
    the failure (file status, quarantined records, event) is persisted in its own transaction

Records are streamed (csv reader -> COPY), so memory does not grow with file size.
Values are stored exactly as in the file: no trimming, no casting, '' is kept as ''.
"""

from __future__ import annotations

import csv
import json
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import OperationalError as SAOperationalError

from olist_platform.errors import DataContractError, ManifestMismatchError, TransientError
from olist_platform.ingestion.manifest import LockEntry
from olist_platform.ingestion.runs import record_event
from olist_platform.utils.logging import get_logger
from olist_platform.validation.contracts import Contract
from olist_platform.validation.schema import check_file, enforce

TASK = "ingest_raw"
MAX_REJECTS_PERSISTED_ON_FAILURE = 1000
NUL = chr(0)
csv.field_size_limit(10 * 1024 * 1024)

log = get_logger(__name__)


@dataclass(frozen=True)
class Reject:
    row_number: int
    rule_name: str
    reason: str
    fields: list[str]


@dataclass
class FileLoadResult:
    table: str
    file: str
    status: str  # loaded | skipped | failed
    source_file_id: int | None = None
    rows_read: int = 0
    rows_loaded: int = 0
    rows_rejected: int = 0
    duration_ms: float = 0.0
    rejects: list[Reject] = field(default_factory=list, repr=False)


def iter_records(path: Path, contract: Contract) -> Iterator[tuple[int, list[str], list[str]]]:
    """Yield (record_number starting at 1, fields, header) for every record after the header."""
    with path.open(encoding=contract.format.encoding, newline="") as fh:
        reader = csv.reader(
            fh,
            delimiter=contract.format.delimiter,
            quotechar=contract.format.quotechar,
            strict=True,
        )
        header = next(reader)
        for number, record in enumerate(reader, start=1):
            yield number, record, header


def structural_problem(record: list[str], header_width: int) -> tuple[str, str] | None:
    """Return (rule_name, reason) if the record cannot be loaded as a row, else None."""
    if len(record) != header_width:
        return "record_structure", f"expected {header_width} fields, found {len(record)}"
    if any(NUL in value for value in record):
        return "record_structure", "field contains a NUL byte"
    return None


def _source_file_row(conn: Connection, contract: Contract, sha256: str) -> tuple[int, str] | None:
    row = conn.execute(
        text(
            "SELECT source_file_id, status FROM meta.source_files "
            "WHERE source_table = :t AND sha256 = :s"
        ),
        {"t": contract.table, "s": sha256},
    ).one_or_none()
    return (row[0], row[1]) if row else None


def _register(
    conn: Connection,
    run_id: uuid.UUID,
    contract: Contract,
    entry: LockEntry,
    sha256: str,
    status: str,
) -> int:
    return conn.execute(
        text(
            "INSERT INTO meta.source_files (source_table, file_name, sha256, size_bytes, "
            "expected_rows, dataset_version, status, registered_run_id) "
            "VALUES (:t, :f, :s, :size, :rows, :v, :status, :run) "
            "ON CONFLICT (source_table, sha256) DO UPDATE SET status = EXCLUDED.status, "
            "rows_loaded = NULL, rows_rejected = NULL, loaded_at = NULL, loaded_run_id = NULL "
            "RETURNING source_file_id"
        ),
        {
            "t": contract.table,
            "f": contract.file,
            "s": sha256,
            "size": entry.size,
            "rows": entry.row_count,
            "v": contract.dataset_version,
            "status": status,
            "run": run_id,
        },
    ).scalar_one()


def _insert_rejects(
    conn: Connection,
    run_id: uuid.UUID,
    contract: Contract,
    source_file_id: int,
    rejects: list[Reject],
    header: list[str],
) -> None:
    if not rejects:
        return
    conn.execute(
        text(
            "INSERT INTO meta.rejected_records (pipeline_run_id, source_file_id, source_table, "
            "record_ref, rule_name, severity, reason, raw_record, layer, action, "
            "source_row_number) "
            "VALUES (:run, :sfid, :t, :ref, :rule, 'ERROR', :reason, CAST(:raw AS jsonb), "
            "'raw', 'quarantined', :row)"
        ),
        [
            {
                "run": run_id,
                "sfid": source_file_id,
                "t": contract.table,
                "ref": f"{contract.file}#record={r.row_number}",
                "rule": r.rule_name,
                "reason": r.reason,
                "row": r.row_number,
                "raw": json.dumps(
                    {"header": header, "fields": [f.replace(NUL, "") for f in r.fields]}
                ),
            }
            for r in rejects
        ],
    )


def _copy_records(
    conn: Connection, run_id: uuid.UUID, contract: Contract, path: Path, source_file_id: int
) -> tuple[int, list[Reject], list[str]]:
    columns = contract.column_names
    target = ", ".join(["_source_file_id", "_row_number", "_pipeline_run_id", *columns])
    dbapi: psycopg.Connection = conn.connection.driver_connection  # same transaction
    rejects: list[Reject] = []
    header: list[str] = []
    rows_read = 0
    try:
        with (
            dbapi.cursor() as cur,
            cur.copy(f"COPY raw.{contract.table} ({target}) FROM STDIN") as cp,
        ):
            positions: list[int] = []
            for number, record, header in iter_records(path, contract):
                if not positions:
                    positions = [header.index(c) for c in columns]  # by name; extras ignored
                rows_read = number
                problem = structural_problem(record, len(header))
                if problem:
                    rejects.append(Reject(number, problem[0], problem[1], record))
                    continue
                cp.write_row((source_file_id, number, run_id, *(record[i] for i in positions)))
    except (csv.Error, UnicodeDecodeError) as exc:
        # Unbalanced quoting or invalid bytes mid-file: record boundaries can no longer be
        # trusted, so no individual record can be quarantined safely -> the file is rejected.
        raise DataContractError(
            f"{contract.file}: unparseable after record {rows_read}: {exc}"
        ) from exc
    return rows_read, rejects, header


def ingest_file(
    engine: Engine,
    run_id: uuid.UUID,
    contract: Contract,
    path: Path,
    entry: LockEntry,
    sha256: str,
    *,
    force_reload: bool = False,
) -> FileLoadResult:
    started = time.perf_counter()
    result = FileLoadResult(contract.table, contract.file, "failed")
    bound = log.bind(task=TASK, source=contract.file, table=contract.table)

    schema = check_file(path, contract)
    try:
        enforce(schema)
    except DataContractError as exc:
        with engine.begin() as conn:
            record_event(
                conn,
                run_id,
                task=TASK,
                event="schema_check_failed",
                status="failed",
                source=contract.file,
                details={"error": str(exc)},
            )
        raise

    try:
        with engine.begin() as conn:
            existing = _source_file_row(conn, contract, sha256)
            if existing and existing[1] == "loaded" and not force_reload:
                result.status, result.source_file_id = "skipped", existing[0]
                record_event(
                    conn,
                    run_id,
                    task=TASK,
                    event="skipped_already_loaded",
                    status="skipped",
                    source=contract.file,
                    source_file_id=existing[0],
                    details={"sha256": sha256},
                )
                bound.info(
                    "file_skipped",
                    status="skipped",
                    reason="already_loaded",
                    source_file_id=existing[0],
                )
                return result

            source_file_id = _register(conn, run_id, contract, entry, sha256, "loading")
            deleted = conn.execute(
                # table name is validated as a plain identifier by the contract model
                text(f"DELETE FROM raw.{contract.table} WHERE _source_file_id = :id"),  # noqa: S608
                {"id": source_file_id},
            ).rowcount
            rows_read, rejects, header = _copy_records(conn, run_id, contract, path, source_file_id)
            result.source_file_id, result.rows_read = source_file_id, rows_read
            result.rows_rejected, result.rejects = len(rejects), rejects
            result.rows_loaded = rows_read - len(rejects)

            if rows_read != entry.row_count:
                raise ManifestMismatchError(
                    f"{contract.file}: read {rows_read} records, lock says {entry.row_count}"
                )
            ratio = len(rejects) / rows_read if rows_read else 0.0
            if ratio > contract.max_reject_ratio:
                raise DataContractError(
                    f"{contract.file}: {len(rejects)} of {rows_read} records rejected "
                    f"({ratio:.2%} > limit {contract.max_reject_ratio:.2%})"
                )

            _insert_rejects(conn, run_id, contract, source_file_id, rejects, header)
            result.duration_ms = round((time.perf_counter() - started) * 1000, 1)
            conn.execute(
                text(
                    "UPDATE meta.source_files SET status = 'loaded', rows_loaded = :loaded, "
                    "rows_rejected = :rejected, loaded_run_id = :run, loaded_at = now() "
                    "WHERE source_file_id = :id"
                ),
                {
                    "loaded": result.rows_loaded,
                    "rejected": result.rows_rejected,
                    "run": run_id,
                    "id": source_file_id,
                },
            )
            record_event(
                conn,
                run_id,
                task=TASK,
                event="file_loaded",
                status="ok",
                source=contract.file,
                source_file_id=source_file_id,
                rows_read=rows_read,
                rows_written=result.rows_loaded,
                rows_rejected=result.rows_rejected,
                duration_ms=result.duration_ms,
                details={"sha256": sha256, "force_reload": force_reload, "rows_replaced": deleted},
            )
        result.status = "loaded"
        bound.info(
            "file_loaded",
            status="ok",
            rows_read=rows_read,
            rows_written=result.rows_loaded,
            rows_rejected=result.rows_rejected,
            duration_ms=result.duration_ms,
            rows_replaced=deleted,
        )
        return result

    except DataContractError as exc:
        _persist_failure(engine, run_id, contract, entry, sha256, result, exc)
        raise
    except (SAOperationalError, psycopg.OperationalError) as exc:
        raise TransientError(f"database unavailable while loading {contract.file}: {exc}") from exc


def _persist_failure(
    engine: Engine,
    run_id: uuid.UUID,
    contract: Contract,
    entry: LockEntry,
    sha256: str,
    result: FileLoadResult,
    exc: Exception,
) -> None:
    """After the data transaction rolled back, keep the evidence: status, rejects, event."""
    result.duration_ms = 0.0
    with engine.begin() as conn:
        source_file_id = _register(conn, run_id, contract, entry, sha256, "failed")
        header = contract.column_names
        _insert_rejects(
            conn,
            run_id,
            contract,
            source_file_id,
            result.rejects[:MAX_REJECTS_PERSISTED_ON_FAILURE],
            header,
        )
        record_event(
            conn,
            run_id,
            task=TASK,
            event="file_rejected",
            status="failed",
            source=contract.file,
            source_file_id=source_file_id,
            rows_read=result.rows_read,
            rows_written=0,
            rows_rejected=result.rows_rejected,
            details={"error": str(exc)},
        )
    log.error(
        "file_rejected",
        task=TASK,
        source=contract.file,
        status="failed",
        rows_read=result.rows_read,
        rows_rejected=result.rows_rejected,
        error_type=type(exc).__name__,
        severity="CRITICAL",
    )
