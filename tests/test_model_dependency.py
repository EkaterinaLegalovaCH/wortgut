"""Regression tests for bug B-1: `./run.sh` (which runs `uv sync`) removed the model.

`uv sync` is exact: anything installed but not in the lockfile gets uninstalled.
The German spaCy model used to be installed by hand, so every launch deleted it
and the first "Read" failed with "spaCy model not installed". These tests keep
the model a declared, locked dependency.
"""

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
PACKAGE = "de-core-news-sm"


def test_model_is_declared_in_pyproject() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert PACKAGE in project["project"]["dependencies"]
    assert PACKAGE in project["tool"]["uv"]["sources"]


def test_model_is_pinned_in_lockfile() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = {package["name"]: package for package in lock["package"]}
    assert PACKAGE in locked
    wheel = locked[PACKAGE]["wheels"][0]
    assert wheel["hash"].startswith("sha256:")


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_uv_sync_would_not_uninstall_the_model() -> None:
    """The exact situation that broke: what would `uv sync` remove right now?"""
    result = subprocess.run(
        ["uv", "sync", "--frozen", "--dry-run"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    output = result.stdout + result.stderr
    removed = [line for line in output.splitlines() if line.strip().startswith("- ")]
    assert not any(PACKAGE in line for line in removed), output
