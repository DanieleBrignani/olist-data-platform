"""A separate OS process that holds the database-wide pipeline lock, for concurrency tests."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

HOLDER = """
import sys
from olist_platform.database.locking import pipeline_lock
with pipeline_lock("test_holder", wait_seconds=0):
    print("held", flush=True)
    sys.stdin.readline()  # hold until the parent closes stdin, or kills this process
print("released", flush=True)
"""


@contextmanager
def other_process_holding_the_lock() -> Iterator[subprocess.Popen]:
    """A separate OS process (own interpreter, own session) that holds the pipeline lock."""
    proc = subprocess.Popen(  # noqa: S603 - fixed interpreter and inline script
        [sys.executable, "-c", HOLDER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),  # includes OLIST_DB_NAME=olist_dw_test
    )
    try:
        wait_for(proc, "held")
        yield proc
    finally:
        if proc.poll() is None:
            proc.stdin.close()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()


def wait_for(proc: subprocess.Popen, marker: str) -> None:
    """Read the child's stdout up to `marker` (its structured log lines come first)."""
    assert proc.stdout is not None
    for line in proc.stdout:
        if line.strip() == marker:
            return
    pytest.fail(f"child exited before printing {marker!r} (exit code {proc.wait()})")
