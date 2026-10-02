"""Error taxonomy. The orchestrator retries on *type*, never on a blanket retry count.

* TransientError     -> infrastructure hiccup; bounded retry with exponential backoff.
* DeterministicError -> same input will fail the same way; never retried.
"""

from __future__ import annotations


class PlatformError(Exception):
    """Base class for all platform errors."""

    error_type: str = "platform_error"


class TransientError(PlatformError):
    """Failure expected to succeed on retry (network, connection reset, lock timeout)."""

    error_type = "transient"


class PipelineBusyError(TransientError):
    """Another operation holds the database-level pipeline lock (database/locking.py).
    Transient: the same operation succeeds once the holder finishes."""

    error_type = "pipeline_busy"


class DeterministicError(PlatformError):
    """Failure that depends only on inputs/code; retrying cannot help."""

    error_type = "deterministic"


class ManifestMismatchError(DeterministicError):
    """Source files differ from the committed manifest lock (ADR-0002)."""

    error_type = "manifest_mismatch"


class DataContractError(DeterministicError):
    """Breaking schema change against a source data contract."""

    error_type = "data_contract_violation"


class QualityGateError(DeterministicError):
    """A CRITICAL data-quality rule failed; publication is blocked (ADR-0004/0005)."""

    error_type = "quality_gate_failed"


def is_transient(exc: BaseException) -> bool:
    """Retry predicate shared by every orchestrated task."""
    return isinstance(exc, TransientError)
