"""Executable dependency boundaries for clean desktop release checkouts."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
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



def test_macos_full_smoke_precedes_packaging_and_cannot_be_skipped():
    workflow = yaml.safe_load(
        (ROOT / '.github/workflows/build-desktop.yml').read_text(encoding='utf-8'))
    steps = workflow['jobs']['build-macos']['steps']
    package_index = next(index for index, step in enumerate(steps)
                         if step.get('name') == 'Create DMG')
    smoke = next(step for step in steps[:package_index]
                 if step.get('env', {}).get('TOFU_SMOKE') == '1')
    assert not smoke.get('continue-on-error')
    assert not smoke.get('if')
    assert './dist/Tofu.app/Contents/MacOS/Tofu' in smoke['run']
    assert smoke['timeout-minutes'] <= 2


def test_frozen_bundle_can_load_the_package_owned_model_contract(tmp_path):
    import runpy
    import shutil
    from tests.desktop.test_desktop_bundle_extension import _spec_datas

    destination = Path('lib/model_info/data')
    entries = [(Path(source), Path(target)) for source, target in _spec_datas()
               if Path(target) == destination]
    assert len(entries) == 1, 'Frozen desktop omitted the provider catalog'
    source, target = entries[0]
    shutil.copytree(source, tmp_path / target)
    loader = tmp_path / 'lib/model_info/_openai_gpt56.py'
    shutil.copy2(ROOT / 'lib/model_info/_openai_gpt56.py', loader)
    contract = runpy.run_path(str(loader))
    assert contract['GPT56_MODEL_IDS']
    assert (tmp_path / target / 'openai.json').is_file()


def test_frozen_bundle_can_load_research_observation_contract(tmp_path):
    import importlib.util
    import shutil
    from tests.desktop.test_desktop_bundle_extension import _spec_datas

    # Materialize the real post-filter data list at the frozen resource root.
    for source, target in _spec_datas():
        if Path(target) != Path('contracts'):
            continue
        source = Path(source)
        destination = tmp_path / target
        destination.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(source, destination / source.name)

    loader = tmp_path / 'lib/research/action_observations.py'
    loader.parent.mkdir(parents=True)
    shutil.copy2(ROOT / 'lib/research/action_observations.py', loader)
    spec = importlib.util.spec_from_file_location(
        'lib.research._frozen_action_observations', loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert not module._validator('binding').is_valid({})

    # Neighboring runtime authorities must survive the same bundle boundary.
    for source in (ROOT / 'contracts').rglob('*'):
        if source.is_file() and source.suffix in {'.json', '.yaml', '.yml'}:
            bundled = tmp_path / 'contracts' / source.relative_to(ROOT / 'contracts')
            assert bundled.read_bytes() == source.read_bytes(), source.name


def test_frozen_html_and_platform_selection_have_their_runtime_assets(tmp_path):
    import runpy
    import shutil
    from tests.desktop.test_desktop_bundle_extension import _spec_datas

    wanted = {'fragments', 'release_assets.py', 'index.html'}
    for source, target in _spec_datas():
        source = Path(source)
        if source.name not in wanted:
            continue
        destination = tmp_path / target
        destination.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(source, destination / source.name)
    loader = tmp_path / 'lib/application_shell_fragments.py'
    loader.parent.mkdir()
    shutil.copy2(ROOT / 'lib/application_shell_fragments.py', loader)
    fragments = runpy.run_path(str(loader))
    html = (tmp_path / 'index.html').read_text(encoding='utf-8')
    assert not fragments['find_markers'](fragments['inject_fragments'](html))
    policy = runpy.run_path(str(tmp_path / 'scripts/release_assets.py'))
    assert policy['PLATFORM_ASSETS'] and policy['AGENT_PLATFORM_ASSETS']


@pytest.mark.parametrize('damage', ('none', 'missing_asset', 'missing_digest'))
@pytest.mark.parametrize('frozen_fragments', (False, True))
def test_frozen_bundle_validates_frontend_without_authoring_sources(
        tmp_path, damage, frozen_fragments, monkeypatch):
    import runpy
    import shutil
    from tests.desktop.test_desktop_bundle_extension import _spec_datas

    entries = [(Path(source), Path(target)) for source, target in _spec_datas()
               if Path(source).name == 'authoring-inputs.json']
    assert len(entries) == 1, 'Frozen desktop omitted Vite runtime metadata'
    source, target = entries[0]
    (tmp_path / target).mkdir(parents=True)
    shutil.copy2(source, tmp_path / target / source.name)
    loader = tmp_path / 'lib/vite_assets.py'
    loader.parent.mkdir(parents=True)
    shutil.copy2(ROOT / 'lib/vite_assets.py', loader)
    assets = runpy.run_path(str(loader))
    assert not (tmp_path / 'frontend/src').exists()
    if frozen_fragments:
        import sys
        monkeypatch.setattr(sys, 'frozen', True, raising=False)
        (tmp_path / 'frontend/src/application-shell/fragments').mkdir(parents=True)

    output = Path(assets['VITE_OUT_DIR'])
    (output / 'assets').mkdir(parents=True)
    manifest = {}
    for name, entry in assets['VITE_ENTRIES'].items():
        filename = f'assets/{name}-test.js'
        (output / filename).write_text('export {};\n', encoding='utf-8')
        manifest[entry] = {'isEntry': True, 'file': filename}
    main = manifest[assets['VITE_ENTRY']]
    main[assets['I18N_CATALOG_DIGEST_FIELD']] = 'a' * 64
    main[assets['VITE_AUTHORING_DIGEST_FIELD']] = 'b' * 64
    if damage == 'missing_asset':
        (output / main['file']).unlink()
    elif damage == 'missing_digest':
        del main[assets['VITE_AUTHORING_DIGEST_FIELD']]
    Path(assets['VITE_MANIFEST']).write_text(json.dumps(manifest), encoding='utf-8')
    if damage == 'none':
        assert assets['validate_vite_artifact']() == manifest
    else:
        with pytest.raises(assets['ViteAssetError']):
            assets['validate_vite_artifact']()
