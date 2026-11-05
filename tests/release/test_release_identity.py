"""Release preparation refuses drift without importing clients or changing files."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import check_monorepo as guard


pytestmark = pytest.mark.unit


@pytest.fixture
def release_tree(tmp_path):
    sources = {
        'VERSION': '4.5.6\n',
        'pyproject.toml': '[project]\nname = "tofu-agent"\nversion = "4.5.6"\n',
        'uv.lock': '[[package]]\nname = "unrelated"\nversion = "0.1.0"\n'
                   '[[package]]\nname = "tofu-agent"\nversion = "4.5.6"\n'
                   'source = { editable = "." }\n',
        'clients/python/pyproject.toml': '[project]\nname = "tofu-sdk"\nversion = "4.5.6"\n',
        'clients/python/tofu_sdk/_version.py': '"""Release identity."""\n__version__ = "4.5.6"\n',
        'clients/typescript/package.json': json.dumps({'name': '@rangehow/tofu-sdk', 'version': '4.5.6'}),
        'clients/typescript/package-lock.json': json.dumps({
            'version': '4.5.6',
            'packages': {'': {'version': '4.5.6'}, 'node_modules/other': {'version': '0.1.0'}},
        }),
        'clients/typescript/src/version.ts': '/** Release identity. */\nexport const VERSION = "4.5.6";\n',
    }
    for relative, source in sources.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return tmp_path


def test_release_identity_checks_selected_tree_without_importing_clients(release_tree):
    assert guard.check_release_identity(release_tree, tag='v4.5.6') == []


@pytest.mark.parametrize('relative', [
    'pyproject.toml', 'uv.lock', 'clients/python/pyproject.toml',
    'clients/python/tofu_sdk/_version.py', 'clients/typescript/package.json',
    'clients/typescript/package-lock.json', 'clients/typescript/src/version.ts',
])
def test_each_release_consumer_must_match_authority(release_tree, relative):
    path = release_tree / relative
    path.write_text(path.read_text().replace('4.5.6', '4.5.5'))
    failures = guard.check_release_identity(release_tree)
    assert failures and all(relative in failure for failure in failures)
    assert all('4.5.6' in failure and '4.5.5' in failure for failure in failures)


@pytest.mark.parametrize('field', ['version', 'packages'])
def test_both_npm_lock_identity_locations_are_checked(release_tree, field):
    path = release_tree / 'clients/typescript/package-lock.json'
    lock = json.loads(path.read_text())
    if field == 'version':
        lock['version'] = '4.5.5'
    else:
        lock['packages']['']['version'] = '4.5.5'
    path.write_text(json.dumps(lock))
    failures = guard.check_release_identity(release_tree)
    assert len(failures) == 1 and 'package-lock.json' in failures[0]


def test_all_well_formed_drift_is_reported_in_one_pass(release_tree):
    (release_tree / 'VERSION').write_text('5.0.0\n')
    failures = guard.check_release_identity(release_tree, tag='v4.5.6')
    assert len(failures) == 9
    assert all('5.0.0' in failure for failure in failures)


@pytest.mark.parametrize('contents', ['', ' ', '4.5.6\n4.5.7'])
def test_absent_or_multiline_authority_is_not_a_release(release_tree, contents):
    (release_tree / 'VERSION').write_text(contents)
    failures = guard.check_release_identity(release_tree)
    assert failures and 'VERSION' in failures[0]


@pytest.mark.parametrize(('relative', 'contents'), [
    ('pyproject.toml', '[project'),
    ('clients/python/pyproject.toml', '[project]\nname="tofu-sdk"'),
    ('clients/typescript/package.json', '{broken'),
    ('clients/typescript/package.json', '[]'),
    ('clients/typescript/package-lock.json', '{"packages": {}}'),
    ('clients/python/tofu_sdk/_version.py', '__version__ = get_version()'),
    ('clients/python/tofu_sdk/_version.py', '__version__ = "4.5.6"\n__version__ += "0"'),
    ('clients/typescript/src/version.ts', 'export const VERSION = getVersion();'),
    ('clients/typescript/src/version.ts', '// export const VERSION = "4.5.6";'),
    ('clients/typescript/src/version.ts', '/* export const VERSION = "4.5.6"; */'),
    ('uv.lock', '[[package]]\nname="tofu-agent"\nversion="4.5.6"\nsource={registry="https://example.invalid"}'),
    ('uv.lock', '[[package]]\nname="other"\nversion="4.5.6"'),
])
def test_invalid_metadata_names_its_source_and_fails_closed(release_tree, relative, contents):
    (release_tree / relative).write_text(contents)
    failures = guard.check_release_identity(release_tree)
    assert failures and any(relative in failure for failure in failures)


def test_duplicate_lock_root_cannot_hide_another_version(release_tree):
    path = release_tree / 'uv.lock'
    path.write_text(path.read_text() + '\n[[package]]\nname="tofu-agent"\nversion="4.5.5"\nsource={editable="."}\n')
    failures = guard.check_release_identity(release_tree)
    assert failures and 'uv.lock' in failures[0]


def test_missing_identity_file_is_an_error(release_tree):
    (release_tree / 'clients/typescript/src/version.ts').unlink()
    failures = guard.check_release_identity(release_tree)
    assert failures and 'clients/typescript/src/version.ts' in failures[0]


def test_runtime_version_source_is_never_executed(release_tree):
    sentinel = release_tree / 'executed'
    source = f'from pathlib import Path\nPath({str(sentinel)!r}).touch()\n__version__ = "4.5.6"\n'
    (release_tree / 'clients/python/tofu_sdk/_version.py').write_text(source)
    assert guard.check_release_identity(release_tree)
    assert not sentinel.exists()


def test_typescript_comments_cannot_hide_duplicate_declarations(release_tree):
    (release_tree / 'clients/typescript/src/version.ts').write_text(
        '/* header */ export const VERSION = "4.5.5";\n'
        '/* note */ export const VERSION = "4.5.6";\n')
    failures = guard.check_release_identity(release_tree)
    assert failures and 'clients/typescript/src/version.ts' in failures[0]


def test_fast_cli_reports_tag_drift_and_leaves_metadata_unchanged(release_tree, monkeypatch, capsys):
    monkeypatch.setattr(guard, 'ROOT', release_tree)
    paths = [path for path in release_tree.rglob('*') if path.is_file()]
    before = {path: path.read_bytes() for path in paths}
    assert guard.main(['--release-identity', '--tag', 'v4.5.6']) == 0
    assert 'release identity: OK' in capsys.readouterr().out
    assert guard.main(['--release-identity', '--tag', 'v9.9.9']) == 1
    assert 'v9.9.9' in capsys.readouterr().out
    assert {path: path.read_bytes() for path in paths} == before


def test_tag_cannot_be_silently_ignored_by_default_cli(monkeypatch):
    def unexpected_repository_check():
        pytest.fail('invalid CLI must be rejected before repository inspection')
    monkeypatch.setattr(guard, 'check', unexpected_repository_check)
    with pytest.raises(SystemExit) as error:
        guard.main(['--tag', 'v4.5.6'])
    assert error.value.code == 2
