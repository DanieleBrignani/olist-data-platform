"""The benchmark's arithmetic and README update are deterministic (numbers are measured by the
script at run time; this only checks how they are summarised and written)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("benchmark", ROOT / "scripts" / "benchmark.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def test_summarise_reports_median_min_max() -> None:
    assert benchmark.summarise([89.1, 69.2, 107.7]) == {"median": 89.1, "min": 69.2, "max": 107.7}


def test_readme_block_is_between_markers_exactly_once() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert readme.count(benchmark.README_START) == 1
    assert readme.count(benchmark.README_END) == 1
    assert readme.index(benchmark.README_START) < readme.index(benchmark.README_END)
    block = readme.split(benchmark.README_START)[1].split(benchmark.README_END)[0]
    assert "scripts/benchmark.py" in block  # generated, not hand-written


def test_committed_results_match_the_readme_block() -> None:
    results = json.loads((ROOT / "benchmark" / "results.json").read_text(encoding="utf-8"))
    _, block = benchmark.render(results)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert block in readme, "README Measured Results differ from benchmark/results.json"
    assert results["deterministic"] is True
