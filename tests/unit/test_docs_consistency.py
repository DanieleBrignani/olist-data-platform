"""Documentation must stay true to the code: every relative link (and Markdown anchor)
resolves, and every documented `make` target and `olist` command exists."""

from __future__ import annotations

import re
from pathlib import Path

import typer.main

from olist_platform.cli import app

ROOT = Path(__file__).resolve().parents[2]
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
FENCE = re.compile(r"```.*?```", re.S)


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    return re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", text))


def _anchors(path: Path) -> set[str]:
    body = FENCE.sub("", path.read_text(encoding="utf-8"))
    return {_slug(h) for h in re.findall(r"^#{1,6}\s+(.+?)\s*$", body, re.M)}


def test_relative_links_and_anchors_resolve() -> None:
    broken = []
    for doc in DOCS:
        for target in LINK.findall(FENCE.sub("", doc.read_text(encoding="utf-8"))):
            if re.match(r"[a-z]+:", target):  # http(s), mailto
                continue
            file_part, _, anchor = target.partition("#")
            resolved = (doc.parent / file_part).resolve() if file_part else doc
            missing_anchor = anchor and resolved.suffix == ".md" and resolved.exists()
            if not resolved.exists() or (missing_anchor and anchor not in _anchors(resolved)):
                broken.append((doc.relative_to(ROOT).as_posix(), target))
    assert broken == []


def _make_targets() -> set[str]:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    return set(re.findall(r"^([a-z0-9-]+):", makefile, re.M))


def test_documented_make_targets_exist() -> None:
    documented = {
        (doc.relative_to(ROOT).as_posix(), t)
        for doc in DOCS
        for t in re.findall(r"`make ([a-z0-9-]+)", doc.read_text(encoding="utf-8"))
    }
    documented |= {
        ("README.md", t)
        for t in re.findall(r"^make ([a-z0-9-]+)", (ROOT / "README.md").read_text("utf-8"), re.M)
    }
    assert documented
    assert {d for d in documented if d[1] not in _make_targets()} == set()


def _is_group(cmd: object) -> bool:
    return hasattr(cmd, "commands")  # typer may vendor click: no isinstance checks


def _command(path: list[str]) -> object | None:
    cmd = typer.main.get_command(app)
    for name in path:
        if not _is_group(cmd) or name not in cmd.commands:
            return None
        cmd = cmd.commands[name]
    return cmd


def test_documented_olist_commands_and_options_exist() -> None:
    problems = []
    found = 0
    for doc in DOCS:
        for line in re.findall(
            r"`olist ([a-z][a-z -]*[a-z](?: --[a-z-]+)*)", doc.read_text("utf-8")
        ):
            words = line.split()
            names = [w for w in words if not w.startswith("--")]
            options = [w for w in words if w.startswith("--")]
            cmd = None
            for n in range(len(names), 0, -1):  # longest valid command prefix
                cmd = _command(names[:n])
                if cmd is not None:
                    break
            found += 1
            if cmd is None or _is_group(cmd):
                problems.append((doc.name, line))
                continue
            known = {o for p in cmd.params for o in getattr(p, "opts", [])}
            known |= {o for p in cmd.params for o in getattr(p, "secondary_opts", [])}
            problems += [(doc.name, line) for o in options if o not in known]
    assert found > 0
    assert problems == []
