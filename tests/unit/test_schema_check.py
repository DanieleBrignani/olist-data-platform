from __future__ import annotations

import codecs
import json
from pathlib import Path

import pytest

from olist_platform.errors import DataContractError
from olist_platform.utils.logging import configure_logging
from olist_platform.validation.contracts import Contract, load_contracts
from olist_platform.validation.schema import check_file, compare_header, enforce

LF, CRLF = "\n", "\r\n"


@pytest.fixture(scope="module")
def sellers() -> Contract:
    return load_contracts()["sellers"]


@pytest.fixture(scope="module")
def translation() -> Contract:
    return load_contracts()["product_category_translation"]


def _write(
    path: Path, header: list[str], rows: list[list[str]], *, eol: str = LF, bom: bool = False
) -> Path:
    lines = [",".join(f'"{h}"' for h in header), *(",".join(r) for r in rows)]
    data = (eol.join(lines) + eol).encode()
    path.write_bytes((codecs.BOM_UTF8 if bom else b"") + data)
    return path


ROW = ["3442f8959a84dea7ee197c632cb2df15", "13023", "campinas", "SP"]


def test_identical_header_passes(sellers: Contract) -> None:
    result = compare_header(sellers, sellers.column_names)
    assert result.ok and not result.non_breaking


def test_missing_column_is_breaking(sellers: Contract) -> None:
    result = compare_header(sellers, sellers.column_names[:-1])
    assert [c.kind for c in result.breaking] == ["missing_columns"]


def test_rename_is_breaking_and_reported_as_possible_rename(sellers: Contract) -> None:
    header = [*sellers.column_names[:-1], "seller_uf"]
    result = compare_header(sellers, header)
    assert not result.ok
    assert "possible rename" in result.breaking[0].detail


def test_extra_column_is_non_breaking(sellers: Contract) -> None:
    result = compare_header(sellers, [*sellers.column_names, "seller_rating"])
    assert result.ok
    assert [c.kind for c in result.non_breaking] == ["extra_columns"]


def test_reordered_columns_are_non_breaking(sellers: Contract) -> None:
    result = compare_header(sellers, list(reversed(sellers.column_names)))
    assert result.ok
    assert [c.kind for c in result.non_breaking] == ["column_order_changed"]


def test_duplicate_header_is_breaking(sellers: Contract) -> None:
    result = compare_header(sellers, [*sellers.column_names, "seller_id"])
    assert "duplicate_columns" in {c.kind for c in result.breaking}


def test_empty_header_is_breaking(sellers: Contract) -> None:
    assert [c.kind for c in compare_header(sellers, []).breaking] == ["empty_header"]


def test_valid_file_passes(tmp_path: Path, sellers: Contract) -> None:
    path = _write(tmp_path / sellers.file, sellers.column_names, [ROW])
    result = check_file(path, sellers)
    assert result.ok and not result.non_breaking


def test_unexpected_bom_is_breaking(tmp_path: Path, sellers: Contract) -> None:
    path = _write(tmp_path / sellers.file, sellers.column_names, [ROW], bom=True)
    result = check_file(path, sellers)
    assert "encoding" in {c.kind for c in result.breaking}


def test_expected_bom_is_stripped_from_first_column(tmp_path: Path, translation: Contract) -> None:
    path = _write(
        tmp_path / translation.file,
        translation.column_names,
        [["beleza_saude", "health_beauty"]],
        eol=CRLF,
        bom=True,
    )
    result = check_file(path, translation)
    assert result.ok, result.breaking
    assert result.header[0] == "product_category_name"


def test_missing_expected_bom_is_breaking(tmp_path: Path, translation: Contract) -> None:
    path = _write(
        tmp_path / translation.file,
        translation.column_names,
        [["beleza_saude", "health_beauty"]],
        eol=CRLF,
    )
    assert "encoding" in {c.kind for c in check_file(path, translation).breaking}


def test_line_terminator_change_is_non_breaking(tmp_path: Path, sellers: Contract) -> None:
    path = _write(tmp_path / sellers.file, sellers.column_names, [ROW], eol=CRLF)
    result = check_file(path, sellers)
    assert result.ok
    assert [c.kind for c in result.non_breaking] == ["line_terminator"]


def test_invalid_utf8_is_breaking(tmp_path: Path, sellers: Contract) -> None:
    path = tmp_path / sellers.file
    path.write_bytes(b'"seller_id","seller_zip_code_prefix","seller_city","seller_s\xe3o"\n')
    assert "encoding" in {c.kind for c in check_file(path, sellers).breaking}


def test_enforce_raises_on_breaking_and_logs_json(
    sellers: Contract, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(environment="test")
    result = compare_header(sellers, [*sellers.column_names[:-1], "extra"])
    with pytest.raises(DataContractError, match="breaking schema change"):
        enforce(result)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    kinds = {(e["event"], e["severity"]) for e in events}
    assert ("schema_change_breaking", "CRITICAL") in kinds
    assert ("schema_change_non_breaking", "WARNING") in kinds


def test_enforce_passes_with_only_non_breaking(sellers: Contract) -> None:
    enforce(compare_header(sellers, [*sellers.column_names, "extra"]))
