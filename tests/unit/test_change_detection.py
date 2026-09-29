from __future__ import annotations

from pathlib import Path

import pytest

from olist_platform.database.change_detection import (
    decide,
    input_fingerprint,
    transform_fingerprint,
)

A, B = "a" * 64, "b" * 64


@pytest.mark.parametrize(
    ("current", "published", "schemas", "full", "rebuild", "reason"),
    [
        (A, A, True, False, False, "inputs_unchanged"),
        (A, B, True, False, True, "inputs_changed"),
        (A, None, True, False, True, "never_published"),
        (A, A, False, False, True, "published_schemas_missing"),
        (A, A, True, True, True, "full_refresh_requested"),
    ],
)
def test_decision_rule(current, published, schemas, full, rebuild, reason) -> None:
    d = decide(current, published, schemas, full)
    assert (d.rebuild, d.reason) == (rebuild, reason)


def _project(root: Path, model_sql: str) -> Path:
    (root / "dbt" / "models").mkdir(parents=True)
    (root / "dbt" / "dbt_project.yml").write_text("name: x\n")
    (root / "dbt" / "models" / "m.sql").write_text(model_sql)
    (root / "data_contracts").mkdir()
    (root / "data_contracts" / "t.yml").write_text("table: t\n")
    return root


def test_transform_fingerprint_changes_with_logic(tmp_path: Path) -> None:
    a = transform_fingerprint(_project(tmp_path / "a", "select 1\n"))
    b = transform_fingerprint(_project(tmp_path / "b", "select 2\n"))
    assert a != b


def test_transform_fingerprint_ignores_line_endings(tmp_path: Path) -> None:
    lf = transform_fingerprint(_project(tmp_path / "lf", "select 1\nfrom t\n"))
    crlf_root = _project(tmp_path / "crlf", "x")
    (crlf_root / "dbt" / "models" / "m.sql").write_bytes(b"select 1\r\nfrom t\r\n")
    assert lf == transform_fingerprint(crlf_root)  # Windows and Linux checkouts agree


def test_input_fingerprint_depends_on_sources_and_logic() -> None:
    base = input_fingerprint({"orders": A}, A)
    assert base == input_fingerprint({"orders": A}, A)
    assert base != input_fingerprint({"orders": B}, A)
    assert base != input_fingerprint({"orders": A}, B)


def test_real_project_fingerprint_is_stable() -> None:
    assert transform_fingerprint() == transform_fingerprint()
