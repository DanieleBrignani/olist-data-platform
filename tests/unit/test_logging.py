from __future__ import annotations

import json
import logging
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from olist_platform.utils.logging import bind_context, clear_context, configure_logging, get_logger


def _records(text: str) -> list[dict]:
    return [json.loads(line) for line in text.strip().splitlines()]


def test_log_lines_are_json_with_bound_run_context(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", environment="test")
    bind_context(pipeline_run_id="run-123", task="ingest_raw")
    try:
        get_logger("olist_platform.test").info(
            "file_loaded", source="olist_orders_dataset.csv", rows_written=10
        )
    finally:
        clear_context()

    record = _records(capsys.readouterr().out)[-1]
    assert record["event"] == "file_loaded"
    assert record["environment"] == "test"
    assert record["pipeline_run_id"] == "run-123"
    assert record["task"] == "ingest_raw"
    assert record["rows_written"] == 10
    assert record["level"] == "info"
    assert record["logger"] == "olist_platform.test"
    assert "timestamp" in record


def test_database_types_serialise_as_json_values(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(environment="test")
    run_id = uuid.UUID("5c0e8d90-ec7c-4092-a540-8a70901c8d99")
    get_logger().info(
        "pipeline_metrics", rows=Decimal("1289091"), ratio=Decimal("0.25"), pipeline_run_id=run_id
    )
    record = _records(capsys.readouterr().out)[-1]
    assert record["rows"] == 1289091  # a number, not "Decimal('1289091')"
    assert record["ratio"] == 0.25
    assert record["pipeline_run_id"] == str(run_id)


def test_third_party_log_records_are_json_too(tmp_path: Path) -> None:
    """Every line of the log FILE must be JSON, including foreign stdlib loggers."""
    log_file = tmp_path / "pipeline.jsonl"
    configure_logging(environment="test", log_file=str(log_file))
    logging.getLogger("alembic.runtime").info("Running upgrade %s -> %s", "0004", "0005")
    logging.getLogger("httpx").info("HTTP Request: POST http://x/api/logs/")  # silenced
    get_logger("olist_platform").info("done")
    for handler in logging.getLogger().handlers:
        handler.flush()

    records = _records(log_file.read_text(encoding="utf-8"))
    assert [r["event"] for r in records] == ["Running upgrade 0004 -> 0005", "done"]
    assert records[0]["logger"] == "alembic.runtime"
    assert records[0]["environment"] == "test"
