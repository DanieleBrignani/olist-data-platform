"""docs/lineage.md is generated from the dbt SQL; it must match the current models."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("lineage", ROOT / "scripts" / "build_lineage.py")
lineage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lineage)


def test_lineage_doc_is_up_to_date() -> None:
    committed = (ROOT / "docs" / "lineage.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert committed == lineage.render(), "run: python scripts/build_lineage.py"


def test_every_model_appears_and_every_published_model_traces_back_to_src() -> None:
    edges = lineage.edges()
    models = {p.stem for p in (ROOT / "dbt" / "models").rglob("*.sql")}
    assert models <= {n for pair in edges for n in pair}
    parents: dict[str, set[str]] = {}
    for parent, child in edges:
        parents.setdefault(child, set()).add(parent)

    def reaches_src(node: str) -> bool:
        return node.startswith("src_") or any(reaches_src(p) for p in parents.get(node, ()))

    published = {m for m in models if m.startswith(("dim_", "fct_", "mart_"))}
    assert published and all(reaches_src(m) for m in published)
