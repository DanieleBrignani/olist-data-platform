"""Structured JSON logging.

Every log line is one JSON object carrying `timestamp`, `level`, `logger`, `event`,
`environment` plus whatever context is bound for the current run (`pipeline_run_id`, `task`,
`source`, ...). Context is bound with `bind_context(...)` and propagates through contextvars,
so library code does not need to pass run ids around.

Records from third-party stdlib loggers (httpx, alembic, ...) go through the same
ProcessorFormatter, so the log file is valid JSON Lines end to end - found when a raw
`HTTP Request: POST ...` line from Prefect's HTTP client broke logs/pipeline.jsonl.
"""

from __future__ import annotations

import datetime as dt
import decimal
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

import structlog

bind_context = structlog.contextvars.bind_contextvars
clear_context = structlog.contextvars.clear_contextvars

# Chatty third-party loggers that would otherwise dominate the pipeline log
QUIET_LOGGERS = ("httpx", "httpcore", "urllib3")


def json_default(value: Any) -> Any:
    """Serialise the types our metadata queries return (numeric -> Decimal, uuid, timestamps)
    as JSON values instead of Python reprs like "Decimal('0')"."""
    if isinstance(value, decimal.Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return repr(value)


def configure_logging(
    level: str = "INFO", environment: str = "local", log_file: str | None = None
) -> None:
    """Idempotent: safe to call from CLI entrypoints, flows and tests."""

    def add_environment(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        event_dict.setdefault("environment", environment)
        return event_dict

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        add_environment,
    ]
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(sort_keys=True, default=json_default),
        ],
    )

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=level.upper(), handlers=handlers, force=True)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
