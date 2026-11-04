"""Generated outputs follow committed and uncommitted source ownership separately."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from tests.project_mod.test_git_land_script import (
    ROOT, SCRIPT, commit_all, feature, git, land, rev, shared, state,
)

pytestmark = pytest.mark.unit
LOCALE = 'frontend/src/i18n/locales/en.json'
CATALOG = 'frontend/src/i18n/generated/en.generated.json'


def generate(repo):
    subprocess.run(['node', 'scripts/gen_i18n_contract.mjs', '--write'], cwd=repo,
                   check=True, capture_output=True, timeout=60)


def edit(repo, key, value):
    path = repo / LOCALE
    source = json.loads(path.read_text())
    source[key] = value
    path.write_text(json.dumps(source, indent=2) + '\n')


@pytest.fixture()
def generated(shared, feature):
    for path in ('frontend/src/i18n/locales', 'frontend/src/i18n/generated', 'scripts'):
        (shared / path).mkdir(parents=True, exist_ok=True)
    (shared / 'frontend/src/i18n/languages.json').write_text(json.dumps({
        'languages': [
            {'code': 'en', 'tag': 'en', 'name': 'English', 'englishName': 'English'},
            {'code': 'fr', 'tag': 'fr', 'name': 'Français', 'englishName': 'French'},
        ],
        'sources': ['en'], 'fallback': 'en', 'default': 'en',
    }) + '\n')
    (shared / LOCALE).write_text(json.dumps(
        {f'key{i}': f'Value{i}' for i in range(10)}, indent=2) + '\n')
    (shared / 'frontend/src/i18n/locales/fr.json').write_bytes((shared / LOCALE).read_bytes())
    (shared / 'scripts/gen_i18n_contract.mjs').write_bytes(
        (ROOT / 'scripts/gen_i18n_contract.mjs').read_bytes())
    generate(shared)
    commit_all(shared, 'locale base')
    git(feature, 'merge', '-q', '--ff-only', 'main')
    edit(feature, 'key0', 'Task')
    generate(feature)
    commit_all(feature, 'task translation')
    return shared, feature


@pytest.mark.parametrize('regenerated', [False, True])
def test_dirty_sources_and_catalogs_are_rebuilt_without_committing_foreign_work(generated, regenerated):
    shared, feature = generated
    before, task = rev(shared), rev(feature)
    edit(shared, 'key9', 'Unfinished')
    if regenerated:
        generate(shared)

    result = land.land('feature', cwd=shared)

    assert result['status'] == 'landed'
    assert rev(shared, 'HEAD^1') == before and rev(shared, 'HEAD^2') == task
    committed = json.loads(git(shared, 'show', f'HEAD:{CATALOG}'))
    assert committed['key0'] == 'Task' and committed['key9'] == 'Value9'
    current = json.loads((shared / CATALOG).read_text())
    assert current['key0'] == 'Task' and current['key9'] == 'Unfinished'
    assert json.loads((shared / LOCALE).read_text()) == current
    assert git(shared, 'diff', '--cached', '--name-only') == ''
    assert set(result['carried']) >= {LOCALE, CATALOG}


def test_generated_dry_run_preserves_every_shared_byte(generated):
    shared, _ = generated
    edit(shared, 'key9', 'Unfinished')
    generate(shared)
    before = state(shared)

    assert land.land('feature', cwd=shared, check=True)['status'] == 'landable'
    assert state(shared) == before


def test_generated_cli_runs_without_pythonpath_or_repository_cwd(generated):
    shared, _ = generated
    edit(shared, 'key9', 'Unfinished')
    generate(shared)
    before = state(shared)
    environment = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}

    result = subprocess.run([sys.executable, str(SCRIPT), 'feature', '--into', str(shared), '--check'],
                            cwd=shared.parent, env=environment, capture_output=True,
                            text=True, timeout=60)

    assert result.returncode == 0, result.stdout + result.stderr
    assert 'can land' in result.stdout
    assert state(shared) == before


def test_manual_generated_edit_without_source_is_preserved_by_refusal(generated):
    shared, _ = generated
    catalog = json.loads((shared / CATALOG).read_text())
    catalog['manual'] = 'Only in the output'
    (shared / CATALOG).write_text(json.dumps(catalog) + '\n')
    before = state(shared)

    with pytest.raises(land.Refused) as refused:
        land.land('feature', cwd=shared)

    assert 'reproduc' in (refused.value.reason + refused.value.advice).lower()
    assert state(shared) == before


def test_authored_conflict_is_never_resolved_by_regenerating(generated):
    shared, _ = generated
    edit(shared, 'key0', 'Conflicting unfinished work')
    generate(shared)
    before = state(shared)

    with pytest.raises(land.Refused) as refused:
        land.land('feature', cwd=shared)

    assert any(LOCALE in path for path in refused.value.paths)
    assert state(shared) == before


def test_generator_failure_leaves_refs_index_and_files_unchanged(generated):
    shared, _ = generated
    edit(shared, 'key9', 'Unfinished')
    generate(shared)
    (shared / LOCALE).write_text('{ invalid json')
    before = state(shared)

    with pytest.raises(land.Refused):
        land.land('feature', cwd=shared)

    assert state(shared) == before


def test_edit_after_generated_planning_replans_from_fresh_sources(generated, monkeypatch):
    shared, _ = generated
    edit(shared, 'key9', 'First edit')
    generate(shared)
    original = land._plan
    edited = []

    def plan_then_edit(*args):
        steps = original(*args)
        if not edited:
            edit(shared, 'key9', 'Second edit')
            generate(shared)
            edited.append(True)
        return steps

    monkeypatch.setattr(land, '_plan', plan_then_edit)
    land.land('feature', cwd=shared)

    current = json.loads((shared / CATALOG).read_text())
    assert current['key0'] == 'Task' and current['key9'] == 'Second edit'


@pytest.mark.parametrize('check', [False, True])
def test_staged_generated_work_refuses_even_in_check_mode(generated, check):
    shared, _ = generated
    edit(shared, 'key9', 'Staged unfinished work')
    generate(shared)
    git(shared, 'add', CATALOG)
    before = state(shared)

    with pytest.raises(land.Refused, match='staged'):
        land.land('feature', cwd=shared, check=check)

    assert state(shared) == before


@pytest.mark.parametrize('new_file', [False, True])
def test_generator_read_set_revalidates_inputs_outside_the_branch_delta(generated, monkeypatch, new_file):
    shared, _ = generated
    original = land._plan
    planned = []
    path = shared / ('frontend/src/i18n/locales/de.json' if new_file
                     else 'frontend/src/i18n/locales/fr.json')

    def plan_then_edit(*args):
        steps = original(*args)
        planned.append(True)
        if len(planned) == 1:
            data = json.loads((shared / LOCALE).read_text())
            data['key9'] = 'Unrelated concurrent translation'
            path.write_text(json.dumps(data, indent=2) + '\n')
            generate(shared)
        return steps

    monkeypatch.setattr(land, '_plan', plan_then_edit)
    land.land('feature', cwd=shared)

    assert len(planned) == 2
    assert json.loads(path.read_text())['key9'] == 'Unrelated concurrent translation'
    if not new_file:
        catalog = shared / 'frontend/src/i18n/generated/fr.generated.json'
        assert json.loads(catalog.read_text())['key9'] == 'Unrelated concurrent translation'
        assert json.loads(git(shared, 'show', 'HEAD:frontend/src/i18n/locales/fr.json'))['key9'] == 'Value9'


def test_new_untracked_language_stays_uncommitted_with_its_catalog(generated):
    shared, _ = generated
    path = shared / 'frontend/src/i18n/languages.json'
    manifest = json.loads(path.read_text())
    manifest['languages'].append({'code': 'de', 'tag': 'de', 'name': 'Deutsch', 'englishName': 'German'})
    path.write_text(json.dumps(manifest) + '\n')
    (shared / 'frontend/src/i18n/locales/de.json').write_bytes((shared / LOCALE).read_bytes())
    generate(shared)

    land.land('feature', cwd=shared)

    assert (shared / 'frontend/src/i18n/generated/de.generated.json').exists()
    assert 'de.generated.json' not in git(shared, 'ls-tree', '-r', '--name-only', 'HEAD')
    assert 'de.json' not in git(shared, 'ls-files')
    assert git(shared, 'diff', '--cached', '--name-only') == ''


def test_unfinished_derived_translation_uses_the_generators_working_fallback(generated):
    shared, _ = generated
    edit(shared, 'unfinished', 'Pending translation')
    generate(shared)

    land.land('feature', cwd=shared)

    fallback = json.loads((shared / 'frontend/src/i18n/generated/fr.generated.json').read_text())
    assert fallback['unfinished'] == 'Pending translation'
    assert 'unfinished' not in json.loads(git(shared, 'show', 'HEAD:' + LOCALE))
    assert 'unfinished' not in json.loads((shared / 'frontend/src/i18n/locales/fr.json').read_text())


def test_deleted_generated_file_without_source_change_is_not_silently_recreated(generated):
    shared, _ = generated
    (shared / CATALOG).unlink()
    before = state(shared)

    with pytest.raises(land.Refused):
        land.land('feature', cwd=shared)

    assert state(shared) == before


def test_generated_rebuild_keeps_uncommitted_mode_changes(generated):
    shared, _ = generated
    (shared / CATALOG).chmod(0o755)

    land.land('feature', cwd=shared)

    assert (shared / CATALOG).stat().st_mode & 0o111
    assert git(shared, 'ls-tree', 'HEAD', '--', CATALOG).startswith('100644 ')
    assert json.loads((shared / CATALOG).read_text())['key0'] == 'Task'


@pytest.mark.parametrize('family', ['styles', 'runtime'])
@pytest.mark.parametrize('committed', [False, True])
def test_composed_outputs_follow_disjoint_source_sections(shared, feature, family, committed):
    from lib.project_mod.generated_integration import GENERATORS

    (shared / '.gitignore').write_text('node_modules\n')
    (shared / '.gitattributes').write_text('static/*.css -merge\nfrontend/src/runtime/*.js -merge\n')
    (shared / 'node_modules').symlink_to(ROOT / 'node_modules', target_is_directory=True)
    (shared / 'scripts').mkdir()
    for script in GENERATORS[family]:
        (shared / script).write_bytes((ROOT / script).read_bytes())
    if family == 'styles':
        (shared / 'static').mkdir()
        for name in ('application', 'settings'):
            root = shared / 'frontend/src/styles' / name
            root.mkdir(parents=True)
            (root / 'manifest.json').write_text(json.dumps({
                'version': 1, 'output': f'static/{"styles" if name == "application" else name}.css',
                'sections': [{'path': 'a.css'}, {'path': 'b.css'}],
            }))
            (root / 'a.css').write_text('.a { color: inherit; }\n')
            (root / 'b.css').write_text('.b { color: inherit; }\n')
        source = 'frontend/src/styles/application'
        first, second, output = f'{source}/a.css', f'{source}/b.css', 'static/styles.css'
    else:
        source = 'frontend/src/runtime/sections'
        root = shared / source
        root.mkdir(parents=True)
        (shared / 'index.html').write_text('<main></main>\n')
        (root / 'manifest.json').write_text(json.dumps({
            'version': 2, 'output': 'frontend/src/runtime/app-runtime.js',
            'prelude': '_prelude.js', 'epilogue': '_epilogue.js', 'lazyBundles': [],
            'sections': [{'source': 'a.js', 'path': 'a.js'}, {'source': 'b.js', 'path': 'b.js'}],
        }))
        for path in ('_prelude.js', '_epilogue.js', 'a.js', 'b.js'):
            (root / path).write_text(f'/* ===== migrated source: {path} ===== */\n')
        first, second, output = f'{source}/a.js', f'{source}/b.js', 'frontend/src/runtime/app-runtime.js'

    def compose(repo):
        result = subprocess.run(['node', GENERATORS[family][0], '--write'], cwd=repo,
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr

    compose(shared)
    commit_all(shared, 'composition base')
    git(feature, 'merge', '-q', '--ff-only', 'main')
    (feature / 'node_modules').symlink_to(ROOT / 'node_modules', target_is_directory=True)
    (feature / first).write_text((feature / first).read_text() + '/* task change */\n')
    compose(feature)
    commit_all(feature, 'task section')
    (shared / second).write_text((shared / second).read_text() + '/* unfinished section */\n')
    compose(shared)
    if committed:
        commit_all(shared, 'concurrent committed section')

    land.land('feature', cwd=shared, keep=True)

    committed_output = git(shared, 'show', f'HEAD:{output}')
    working = (shared / output).read_text()
    assert 'task change' in committed_output
    assert ('unfinished section' in committed_output) is committed
    assert 'task change' in working and 'unfinished section' in working
    assert (shared / second).read_text().endswith('/* unfinished section */\n')
    assert git(shared, 'diff', '--cached', '--name-only') == ''
