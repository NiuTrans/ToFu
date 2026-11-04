"""Executable specification for first-party monorepo boundaries."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Intentional references to paths that do not (and must not)
# exist in the real repo — negative-case fixture anchors.
_AUDIT_SYNTHETIC_REPO_PATHS = {'scripts/check.py', 'scripts/check_release.py', 'scripts/client.py', 'scripts/generated_contract.py', 'tests/test_plain.py', 'tests/test_release.py'}


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_monorepo_contract_guard_passes():
    result = subprocess.run(
        [sys.executable, "scripts/check_monorepo.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tofu-agent, tofu-search, tofu-trading" in result.stdout


def test_declared_script_cannot_be_satisfied_by_untracked_local_file(tmp_path):
    from scripts.check_monorepo import check_declared_scripts
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'Makefile').write_text('check:\n\tpython3 scripts/check.py\n')
    (tmp_path / 'scripts/check.py').write_text('print("ok")\n')
    assert check_declared_scripts(tmp_path, set()) == [
        'declared build script is not tracked: scripts/check.py']
    assert check_declared_scripts(tmp_path, {'scripts/check.py'}) == []


def test_declared_script_must_exist_even_when_git_tracks_its_name(tmp_path):
    from scripts.check_monorepo import check_declared_scripts
    (tmp_path / 'package.json').write_text('{"scripts":{"build":"node scripts/build.mjs"}}')
    assert check_declared_scripts(tmp_path, {'scripts/build.mjs'}) == [
        'declared build script is missing: scripts/build.mjs']


def test_build_script_dependencies_are_part_of_clean_checkout_contract(tmp_path):
    from scripts.check_monorepo import check_declared_scripts
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'package.json').write_text('{"scripts":{"build":"node scripts/build.mjs"}}')
    (tmp_path / 'scripts/build.mjs').write_text("import { retain } from './retention.mjs';\n")
    (tmp_path / 'scripts/retention.mjs').write_text("import './build.mjs';\n")
    tracked = {'scripts/build.mjs'}
    assert check_declared_scripts(tmp_path, tracked) == [
        'declared build script is not tracked: scripts/retention.mjs']
    assert check_declared_scripts(tmp_path, tracked | {'scripts/retention.mjs'}) == []


def test_tracked_python_script_requires_its_generated_import(tmp_path):
    from scripts.check_monorepo import check_declared_scripts
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'scripts/client.py').write_text(
        'from scripts.generated_contract import VERSION\n')
    tracked = {'scripts/client.py'}
    assert check_declared_scripts(tmp_path, tracked) == [
        'declared build script is missing: scripts/generated_contract.py']
    (tmp_path / 'scripts/generated_contract.py').write_text('VERSION = 1\n')
    assert check_declared_scripts(tmp_path, tracked) == [
        'declared build script is not tracked: scripts/generated_contract.py']
    assert check_declared_scripts(tmp_path, tracked | {'scripts/generated_contract.py'}) == []


@pytest.mark.parametrize('statement', [
    'from scripts.check_release import verify',
    'from scripts import check_release',
    'import scripts.check_release',
])
def test_retained_test_imports_require_tracked_script_owners(tmp_path, statement):
    from scripts.check_monorepo import check_declared_scripts
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'tests/test_release.py').write_text(statement + '\n')
    tracked = {'tests/test_release.py'}
    assert check_declared_scripts(tmp_path, tracked) == [
        'declared build script is missing: scripts/check_release.py']
    (tmp_path / 'scripts/check_release.py').write_text('def verify(): pass\n')
    assert check_declared_scripts(tmp_path, tracked) == [
        'declared build script is not tracked: scripts/check_release.py']
    assert check_declared_scripts(tmp_path, tracked | {'scripts/check_release.py'}) == []


def test_script_mentions_only_narrow_which_retained_tests_are_read(tmp_path):
    from scripts.check_monorepo import check_declared_scripts
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_release.py').write_text('from scripts import check_release\n')
    (tmp_path / 'tests/test_plain.py').write_text('def test_plain():\n    pass\n')
    tracked = {'tests/test_release.py', 'tests/test_plain.py'}
    missing = ['declared build script is missing: scripts/check_release.py']
    assert check_declared_scripts(tmp_path, tracked) == missing
    assert check_declared_scripts(
        tmp_path, tracked, script_mentions={'tests/test_release.py'}) == missing
    assert check_declared_scripts(tmp_path, tracked, script_mentions=set()) == []


_TABLE = ['pyproject.toml: project.license must be an SPDX expression string, not a table']
_OLD_FLOOR = ['pyproject.toml: an SPDX project.license needs build-system setuptools>=77']
_CLASSIFIER = ['pyproject.toml: remove License :: classifiers; setuptools rejects them '
               'beside an SPDX project.license']


@pytest.mark.parametrize(('license_line', 'requirement', 'expected'), [
    ('license = {text = "MIT"}', 'setuptools==80.10.2', _TABLE),
    ('license = {file = "LICENSE"}', 'setuptools==80.10.2', _TABLE),
    ('license = "MIT"', 'setuptools>=68.0', _OLD_FLOOR),
    ('license = "MIT"', 'setuptools<81', _OLD_FLOOR),
    ('license = "MIT"', 'setuptools-scm>=80', _OLD_FLOOR),
    ('license = "MIT"', 'setuptools>=77,<81', []),
    ('license = "MIT AND Apache-2.0"', 'setuptools==80.10.2', []),
    ('license = "MIT"\nclassifiers = ["License :: OSI Approved :: MIT License"]',
     'setuptools==80.10.2', _CLASSIFIER),
    ('license = "MIT"\nclassifiers = ["Programming Language :: Python :: 3"]',
     'setuptools==80.10.2', []),
    ('', 'setuptools>=68', []),
])
def test_package_license_stays_buildable_by_current_setuptools(
        tmp_path, license_line, requirement, expected):
    from scripts.check_monorepo import check_package_license_metadata
    (tmp_path / 'pyproject.toml').write_text(
        f'[build-system]\nrequires = ["{requirement}", "wheel"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        f'[project]\nname = "member"\nversion = "1.0"\n{license_line}\n')
    assert check_package_license_metadata(tmp_path, {'pyproject.toml'}) == expected
