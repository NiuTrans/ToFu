"""Executable dependency boundaries for clean desktop release checkouts."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit


def _frontend_script_paths() -> set[str]:
    package = json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))
    paths = {
        match
        for command in package['scripts'].values()
        for match in re.findall(r'(?:node|python3?)\s+(scripts/[^\s;&]+)', command)
    }
    # The build imports the TDZ checker and invokes the browser canary rather
    # than listing both as separate npm scripts. They must also survive export.
    paths.update({
        'scripts/check_frontend_tdz.mjs',
        'scripts/frontend_bundle_canary.py',
    })
    return paths


def test_frontend_release_commands_have_their_source_files():
    missing = sorted(path for path in _frontend_script_paths()
                     if not (ROOT / path).is_file())
    assert not missing, f'Frontend release commands reference missing files: {missing}'


def test_opensource_export_keeps_frontend_release_commands():
    authority = ROOT / 'export_pkg' / '_lists.py'
    if not authority.is_file():
        pytest.skip('Private export policy is intentionally not in public source')
    module = ast.parse(authority.read_text(encoding='utf-8'))
    assignment = next(
        node for node in module.body if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name)
                and target.id == '_OPENSOURCE_KEEP_FILES'
                for target in node.targets)
    )
    keep = ast.literal_eval(assignment.value)
    missing = sorted(_frontend_script_paths() - keep)
    assert not missing, f'Public export strips frontend build prerequisites: {missing}'


def test_frontend_release_provisions_contract_parser_and_browser_canary():
    workflow = yaml.safe_load(
        (ROOT / '.github/workflows/build-desktop.yml').read_text(encoding='utf-8'))
    steps = workflow['jobs']['frontend-dist']['steps']
    build_index = next(index for index, step in enumerate(steps)
                       if 'npm run check:frontend' in step.get('run', ''))
    prerequisites = steps[:build_index]
    assert any(str(step.get('uses', '')).startswith('actions/setup-python@')
               for step in prerequisites)
    commands = '\n'.join(step.get('run', '') for step in prerequisites)
    assert re.search(r'pip install[^\n]*PyYAML', commands)
    assert re.search(r'pip install[^\n]*playwright', commands)
    assert 'python -m playwright install --with-deps chromium' in commands



def test_every_agent_platform_enforces_its_declared_size_floor():
    import runpy

    policy = runpy.run_path(str(ROOT / 'scripts/release_assets.py'))
    for _os, _arch, _label, pattern, floor in policy['AGENT_PLATFORM_ASSETS']:
        filename = pattern.replace('*', '0.17.4')
        assert policy['undersized_assets']({filename: floor - 1}), filename
        assert not policy['undersized_assets']({filename: floor}), filename


def test_macos_agent_smoke_is_required_before_packaging():
    workflow = yaml.safe_load(
        (ROOT / '.github/workflows/build-desktop.yml').read_text(encoding='utf-8'))
    steps = workflow['jobs']['build-macos']['steps']
    package_index = next(index for index, step in enumerate(steps)
                         if step.get('name') == 'Create agent DMG')
    smoke = next(step for step in steps[:package_index]
                 if step.get('env', {}).get('TOFU_AGENT_SMOKE') == '1')
    assert not smoke.get('continue-on-error')
    assert not smoke.get('if')
    assert './dist/TofuAgent.app/Contents/MacOS/TofuAgent' in smoke['run']
    assert 'TOFU_AGENT_SMOKE_OK' in smoke['run']
    assert 'exit 1' in smoke['run']
