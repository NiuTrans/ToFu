"""Executable contract for the Python 3.12 and frozen-uv migration baseline."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
NAME = re.compile(r"^([A-Za-z0-9_.-]+)(.*)$")


def _dependency_map(specifications: list[str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_specification in specifications:
        specification = raw_specification.strip()
        match = NAME.fullmatch(specification)
        assert match, f"invalid dependency specification: {specification}"
        package_name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
        assert package_name not in normalized, f"duplicate dependency: {package_name}"
        normalized[package_name] = match.group(2).replace(" ", "")
    return normalized


def _requirements_dependencies() -> list[str]:
    return [
        line.strip()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_pyproject_is_python_312_dependency_authority():
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]

    assert project["requires-python"] == ">=3.12"
    assert "dynamic" not in project
    personal_dependencies = [
        *project["dependencies"],
        *project["optional-dependencies"]["app"],
        *project["optional-dependencies"]["worker"],
    ]
    assert _dependency_map(personal_dependencies) == _dependency_map(
        _requirements_dependencies())
    assert (ROOT / ".python-version").read_text(encoding="utf-8").strip() == "3.12"
    assert "Python 3.12" in (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Python 3.12" in (ROOT / "README_CN.md").read_text(encoding="utf-8")


def test_tooling_and_container_exclude_nested_codex_tree():
    ruff = (ROOT / "ruff.toml").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert 'target-version = "py312"' in ruff
    assert 'extend-exclude = ["codex"]' in ruff
    assert "codex/" in dockerignore


def test_primary_ci_uses_frozen_uv_and_blocks_slow_failures():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert 'python-version: "3.10"' not in workflow
    assert 'python-version: ["3.10", "3.12"]' not in workflow
    assert "continue-on-error: true" not in workflow
    assert "uv sync --frozen" in workflow
    assert "pyright" in workflow
    assert (ROOT / "pyrightconfig.json").is_file()
    for required_gate in (
        "make docs-check",
        "make architecture-check",
        "make contracts-check",
        "make suite-health",
        "make healthcheck",
    ):
        assert required_gate in workflow
