"""CLI contract: operators and CI rely on exit codes (0 = ok, 1 = a platform failure)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from olist_platform.cli import app
from olist_platform.ingestion.source import raw_dir
from olist_platform.validation.contracts import load_contracts
from tests.synthetic import base_rows, write_dataset

runner = CliRunner()
CONTRACTS = load_contracts()


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    write_dataset(raw_dir(tmp_path), base_rows(), CONTRACTS)
    return tmp_path


def test_help_lists_every_pipeline_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in (
        "ingest",
        "stage",
        "transform",
        "warehouse",
        "run",
        "rollback-publish",
        "exporter",
        "fingerprint",
        "contracts",
        "source",
        "db",
        "doctor",
    ):
        assert command in result.output


def test_contracts_validate_succeeds() -> None:
    assert runner.invoke(app, ["contracts", "validate"]).exit_code == 0


def test_contracts_docs_writes_the_generated_page(tmp_path: Path) -> None:
    out = tmp_path / "contracts.md"
    assert runner.invoke(app, ["contracts", "docs", "--out", str(out)]).exit_code == 0
    assert out.read_text(encoding="utf-8").startswith("# Source data contracts")


def test_check_source_passes_on_conforming_files(data_root: Path) -> None:
    result = runner.invoke(app, ["contracts", "check-source", "--data-root", str(data_root)])
    assert result.exit_code == 0


def test_check_source_exits_1_on_a_breaking_change(data_root: Path) -> None:
    sellers = raw_dir(data_root) / "olist_sellers_dataset.csv"
    sellers.write_text(sellers.read_text().replace("seller_state", "seller_uf", 1), newline="")
    result = runner.invoke(app, ["contracts", "check-source", "--data-root", str(data_root)])
    assert result.exit_code == 1


def test_check_source_exits_1_when_a_file_is_missing(data_root: Path) -> None:
    (raw_dir(data_root) / "olist_orders_dataset.csv").unlink()
    result = runner.invoke(app, ["contracts", "check-source", "--data-root", str(data_root)])
    assert result.exit_code == 1


def test_source_fetch_locates_existing_files_without_network(data_root: Path) -> None:
    result = runner.invoke(app, ["source", "fetch", "--data-root", str(data_root)])
    assert result.exit_code == 0


def test_source_manifest_writes_manifest_once(data_root: Path) -> None:
    args = ["source", "manifest", "--data-root", str(data_root)]
    assert runner.invoke(app, args).exit_code == 0
    manifest = raw_dir(data_root) / "manifest.json"
    assert manifest.is_file()
    assert runner.invoke(app, args).exit_code == 0  # second call keeps the immutable file
    shutil.rmtree(raw_dir(data_root))
