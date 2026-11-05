"""First-party distributions in one checkout must be co-installable.

``tests/conftest.py`` binds first-party imports to their checkout sources, so a
first-party requirement whose range excludes the sibling's checkout version is
invisible to every test, while an installation resolves an older release that
the requiring code no longer works with (for example a provider importing a
module its pinned SDK release does not have).
"""
from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]


def _distributions() -> dict[str, tuple[str, dict]]:
    listed = subprocess.run(['git', 'ls-files', '*pyproject.toml'], cwd=_ROOT, text=True,
                            capture_output=True, check=True).stdout.split()
    found = {}
    for path in listed:
        project = tomllib.loads((_ROOT / path).read_text(encoding='utf-8')).get('project') or {}
        if project.get('name'):
            found[canonicalize_name(project['name'])] = (path, project)
    return found


def test_first_party_requirements_admit_the_checkout_versions():
    distributions = _distributions()
    assert {'tofu-environments', 'tofu-meituan-environments'} <= distributions.keys()
    excluded = []
    for path, project in distributions.values():
        requirements = list(project.get('dependencies') or [])
        for extra in (project.get('optional-dependencies') or {}).values():
            requirements.extend(extra)
        for raw in requirements:
            requirement = Requirement(raw)
            sibling = distributions.get(canonicalize_name(requirement.name))
            if sibling is None:
                continue
            version = sibling[1].get('version')
            if not requirement.specifier.contains(version, prereleases=True):
                excluded.append(f'{path}: {raw!r} excludes {sibling[0]} version {version}')
    assert not excluded, '\n'.join(excluded)
