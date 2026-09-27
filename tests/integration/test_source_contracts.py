"""The real Olist v2 files conform to their contracts at file/schema level.

Needs the dataset (`olist source fetch`); skipped with an explicit reason otherwise so that
unit-only environments stay green without hiding the requirement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from olist_platform.ingestion.source import raw_dir
from olist_platform.validation.contracts import Contract, load_contracts
from olist_platform.validation.schema import check_file

pytestmark = pytest.mark.source_data

DATA_DIR = raw_dir(Path(__file__).resolve().parents[2] / "data")
CONTRACTS = load_contracts()


@pytest.mark.parametrize("contract", CONTRACTS.values(), ids=list(CONTRACTS))
def test_real_file_matches_contract_exactly(contract: Contract) -> None:
    path = DATA_DIR / contract.file
    if not path.is_file():
        pytest.skip(f"{path} not present; run `olist source fetch`")
    result = check_file(path, contract)
    assert result.ok, result.breaking
    # The contract was written from these files: not even a non-breaking drift is expected.
    assert not result.non_breaking, result.non_breaking
