from __future__ import annotations

import pytest

from olist_platform.errors import (
    DataContractError,
    ManifestMismatchError,
    QualityGateError,
    TransientError,
    is_transient,
)


def test_only_transient_errors_are_retried() -> None:
    assert is_transient(TransientError("connection reset"))


@pytest.mark.parametrize(
    "exc",
    [
        ManifestMismatchError("sha256 differs"),
        DataContractError("column removed"),
        QualityGateError("critical rule failed"),
        ValueError("unexpected bug"),
    ],
)
def test_deterministic_and_unknown_errors_are_never_retried(exc: Exception) -> None:
    assert not is_transient(exc)
