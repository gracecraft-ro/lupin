"""docs/configuration.md must list each LUPIN_ variable that src/ uses.

Sources: every `.py` and `.json` file under `src/`. `flake.nix` and
`pyproject.toml` set no variable, so this test does not read them.
A documented variable is a Markdown heading that names it in backticks,
for example "### `LUPIN_REDIS_HOST`".
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "configuration.md"
NAME = re.compile(r"LUPIN_[A-Z_]+")
HEADING = re.compile(r"^#{2,6}\s+`(LUPIN_[A-Z_]+)`\s*$", re.MULTILINE)


def source_variables(src: Path = ROOT / "src") -> set[str]:
    names: set[str] = set()
    for path in sorted(src.rglob("*")):
        if path.suffix in {".py", ".json"} and path.is_file():
            names.update(NAME.findall(path.read_text(encoding="utf-8")))
    return names


def documented_variables(doc: Path = DOC) -> set[str]:
    return set(HEADING.findall(doc.read_text(encoding="utf-8")))


def mentioned_variables(doc: Path = DOC) -> set[str]:
    return set(NAME.findall(doc.read_text(encoding="utf-8")))


def test_source_has_variables():
    # Guards the scan itself: an empty set would make the next test pass.
    assert "LUPIN_REDIS_HOST" in source_variables()


def test_every_source_variable_has_an_entry():
    missing = sorted(source_variables() - documented_variables())
    assert not missing, f"add a heading to docs/configuration.md for: {missing}"


def test_every_documented_variable_is_in_source():
    stale = sorted(mentioned_variables() - source_variables())
    assert not stale, f"docs/configuration.md names variables that src/ does not use: {stale}"
