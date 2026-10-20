"""Frontend dependency installs share one Node selection and enforce engines.

Inputs: workflow jobs, repository npm configuration, package and lock metadata.
Runtime compatibility is exercised by the same strict npm ci used in CI.
"""
from __future__ import annotations

import configparser
import json
from pathlib import Path
import re

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def _frontend_install_jobs():
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        for name, job in workflow.get("jobs", {}).items():
            steps = job.get("steps", [])
            installs = [
                index for index, step in enumerate(steps)
                if re.search(r"\bnpm\s+ci\b", str(step.get("run", "")))
                and step.get("working-directory", ".") == "."
            ]
            if installs:
                yield path.name, name, steps, installs


@pytest.mark.parametrize(
    "workflow,job,steps,installs", list(_frontend_install_jobs()),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_frontend_installs_read_the_shared_node_selection(workflow, job, steps, installs):
    selection = ROOT / ".node-version"
    assert selection.is_file(), "Frontend CI needs a repository-owned Node selection"
    assert selection.read_text().strip(), "The shared Node selection must not be empty"
    for index in installs:
        setups = [
            step for step in steps[:index]
            if str(step.get("uses", "")).startswith("actions/setup-node@")
        ]
        assert setups, f"{workflow}/{job}: npm ci has no preceding Node setup"
        options = setups[-1].get("with", {})
        assert options.get("node-version-file") == selection.name, (
            f"{workflow}/{job}: Node selection differs from the repository"
        )
        assert not options.get("node-version"), (
            f"{workflow}/{job}: a literal Node version overrides the shared file"
        )


def test_repository_installs_reject_incompatible_engines():
    path = ROOT / ".npmrc"
    assert path.is_file(), "npm must reject incompatible engines before building"
    config = configparser.ConfigParser()
    config.read_string("[npm]\n" + path.read_text())
    assert config.getboolean("npm", "engine-strict", fallback=False)


def test_package_and_lock_agree_on_supported_node_versions():
    package = json.loads((ROOT / "package.json").read_text())
    locked = json.loads((ROOT / "package-lock.json").read_text())["packages"][""]
    assert package["engines"]["node"] == locked["engines"]["node"]
