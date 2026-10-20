"""The version has one source (issue #144)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from lupin import cli

ROOT = Path(__file__).resolve().parents[1]


def test_version_flag_prints_the_pyproject_version(capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["--version"])

    assert caught.value.code == 0
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert capsys.readouterr().out.strip() == f"lupin {version}"


def test_the_flake_does_not_hardcode_the_version():
    flake = (ROOT / "flake.nix").read_text(encoding="utf-8")

    assert "builtins.fromTOML (builtins.readFile ./pyproject.toml)" in flake
    assert 'version = "0.1.0"' not in flake
