"""Impact selection: unit fingerprints, the recorder plugin and affected-file planning.

The recorder runs in real pytest subprocesses (serial and under xdist) over a
synthetic project under tmp_path; nothing reads or writes the repository that
runs this suite.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lib.project_mod import impact_selection as impact
from lib.storage_authority_sql.impact_cache import SCHEMA_VERSION

pytestmark = pytest.mark.unit

_AUDIT_SYNTHETIC_REPO_PATHS = {
    'tests/_harness.py', 'tests/test_a.py', 'tests/test_b.py', 'tests/test_c.py',
    'tests/test_d.py', 'tests/test_e.py', 'tests/test_calc.py', 'tests/test_far.py', 'tests/test_new.py',
}

BASE_SOURCE = ('"""Calc."""\nimport os\n\nLIMIT = 1\n\n\nclass Store:\n    kind = "a"\n\n'
               '    def get(self):\n        return LIMIT\n\n\ndef helper():\n    """Help."""\n'
               '    def inner():\n        return 1\n    return inner()\n')


def test_units_split_function_bodies_from_the_module_body():
    before = impact.unit_fingerprints(BASE_SOURCE)
    body_edit = impact.unit_fingerprints(BASE_SOURCE.replace('return LIMIT', 'return LIMIT + 0'))
    moved = impact.unit_fingerprints('# a comment\n' + BASE_SOURCE.replace('\n\n\ndef', '\n\n\n\n\ndef'))
    constant = impact.unit_fingerprints(BASE_SOURCE.replace('LIMIT = 1', 'LIMIT = 2'))
    docstring = impact.unit_fingerprints(BASE_SOURCE.replace('"""Help."""', '"""Assist."""'))

    assert set(before) == {'<module>', 'Store.get', 'helper'}
    assert moved == before
    assert body_edit['Store.get'] != before['Store.get']
    assert (body_edit['<module>'], body_edit['helper']) == (before['<module>'], before['helper'])
    assert constant['<module>'] != before['<module>']
    assert docstring['<module>'] != before['<module>'], 'decorators may read docstrings'
    assert impact.unit_fingerprints('def broken(:\n') is None


def test_code_object_names_map_to_their_outermost_unit():
    assert impact.unit_of('helper') == 'helper'
    assert impact.unit_of('helper.<locals>.inner') == 'helper'
    assert impact.unit_of('Store.get.<locals>.<lambda>') == 'Store.get'
    assert impact.unit_of('<module>') == '<module>'
    assert impact.unit_of('<lambda>') == '<module>'
    assert impact.unit_of('Store.<lambda>') == '<module>'


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _project(root: Path) -> None:
    _write(root, 'pkg/__init__.py', '')
    _write(root, 'pkg/calc.py', 'def scale(x):\n    return x * 2\n\n\n'
           'def unused():\n    return 0\n\n\ndef setup_value():\n    return 5\n')
    _write(root, 'pkg/table.py', 'def build():\n    return {"a": 1}\n\n\nTABLE = build()\n')
    _write(root, 'data/expected.txt', '4\n')
    _write(root, 'tests/conftest.py',
           'import pytest\nfrom pkg.calc import setup_value\n\n\n'
           '@pytest.fixture(scope="session")\ndef base():\n    return setup_value()\n')
    _write(root, 'tests/test_a.py',
           'import os, pathlib\nfrom pkg.calc import scale\nfrom pkg.table import TABLE\n\n\n'
           'def test_scale(base):\n'
           '    data = pathlib.Path(__file__).parent.parent / "data"\n'
           '    assert sorted(os.listdir(data)) == ["expected.txt"]\n'
           '    assert scale(2) == int((data / "expected.txt").read_text()) and base == 5\n'
           '    assert TABLE == {"a": 1}\n')
    _write(root, 'tests/test_b.py',
           'import subprocess, sys\n\n\ndef test_child(base):\n'
           '    subprocess.run([sys.executable, "-c", "pass"], check=True)\n'
           '    assert base == 5\n\n\ndef test_fails():\n    assert False\n')
    # Provision outside pytest: tmp_path's external directory census is itself
    # an unobserved input, separate from the Git subprocess classification.
    external_git = root.parent / 'external-git'
    external_git.mkdir(exist_ok=True)
    _write(root, 'tests/test_c.py',
           'import subprocess\n\n\ndef test_git_elsewhere():\n'
           f'    subprocess.run(["git", "-C", {str(external_git)!r}, "init", "-q"], check=True)\n')
    _write(root, 'tests/test_d.py',
           'import pathlib, subprocess\n\n\ndef test_reads_the_repository():\n'
           '    subprocess.run(["git", "ls-files"], capture_output=True)\n'
           '    assert "def scale" in pathlib.Path("pkg/calc.py").read_text()\n')
    _write(root, 'tests/test_e.py',
           'from pkg.table import TABLE\n\n\ndef test_table():\n    assert TABLE["a"] == 1\n')


TESTS = ['tests/test_a.py', 'tests/test_b.py', 'tests/test_c.py', 'tests/test_d.py',
         'tests/test_e.py']
REFS = {'tests/test_a.py': {'pkg/calc.py', 'pkg/table.py'}, 'tests/test_b.py': {'pkg/calc.py'},
        'tests/test_e.py': {'pkg/table.py'}}


def _observe(project: Path, tmp_path: Path, *extra: str) -> list[dict]:
    plugin = tmp_path / 'plugin'
    plugin.mkdir(exist_ok=True)
    (plugin / 'tofu_test_impact_plugin.py').write_text(impact.plugin_source(_tree(project).blobs))
    out = tmp_path / f'out{len(extra)}'
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(PYTHONPATH=os.pathsep.join([str(plugin), str(project)]),
               PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', TOFU_TEST_IMPACT_OUT=str(out),
               TOFU_TEST_IMPACT_ROOT=str(project))
    subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
                    '-p', 'tofu_test_impact_plugin', *extra, *TESTS],
                   cwd=project, env=env, capture_output=True, text=True, timeout=180)
    return [json.loads(line) for path in sorted(out.glob('*.jsonl'))
            for line in path.read_text().splitlines()]


def _tree(project: Path) -> impact.Tree:
    paths = [str(path.relative_to(project)) for path in project.rglob('*')
             if path.is_file() and '__pycache__' not in path.parts]
    return impact.Tree({path: f'blob-{path}' for path in paths},
                       source=lambda path: (project / path).read_text())


@pytest.mark.parametrize('extra', [(), ('-p', 'xdist.plugin', '-n', '2')],
                         ids=['serial', 'xdist'])
def test_the_recorder_attributes_units_fixtures_reads_imports_and_programs(tmp_path, extra):
    project = tmp_path / 'project'
    _project(project)
    lines = _observe(project, tmp_path, *extra)

    records = {record.test: record for record in impact.records_from_observations(
        lines, TESTS, _tree(project), 'tree', REFS)}

    assert set(records) == set(TESTS)
    a, b, c, d, e = (records[test] for test in TESTS)
    assert a.deps['pkg/calc.py'] == ('blob-pkg/calc.py', ['<module>', 'scale', 'setup_value']), (
        'executed units, a session fixture it uses, and the module body its code reads')
    assert a.deps['data/expected.txt'] == ('blob-data/expected.txt', None)
    assert a.deps['tests/conftest.py'][1] == sorted(
        {impact.CONFTEST_UNIT, impact.MODULE_UNIT, 'base'}), (
        'import-time code and the discovered fixture names, plus the units this run executed')
    assert a.deps['tests/test_a.py'][1] is None
    assert set(a.dirs) == {'data'}, 'the import system lists directories too; those do not count'
    assert (a.outcome, a.opaque) == ('passed', False)
    assert e.deps['pkg/table.py'] == ('blob-pkg/table.py', ['<module>', 'build']), (
        'build() ran while another file imported pkg.table; it shaped TABLE for this file too')
    assert b.opaque and b.deps['pkg/calc.py'] == ('blob-pkg/calc.py', None), (
        'a Python child process is unobserved, so what the file references counts whole')
    assert (b.outcome, b.failed) == ('failed', ['tests/test_b.py::test_fails'])
    assert not c.opaque, 'git run outside the repository is observed through its effects'
    assert d.opaque, 'git reading the repository is not observed'
    assert d.deps['pkg/calc.py'] == ('blob-pkg/calc.py', None), 'source read as text counts whole'


@pytest.mark.parametrize('extra', [(), ('-p', 'xdist.plugin', '-n', '2')],
                         ids=['serial', 'xdist'])
def test_cached_dynamic_session_fixtures_keep_their_inputs_for_every_consumer(tmp_path, extra):
    project = tmp_path / 'project'
    _project(project)
    _write(project, 'data/session.txt', 'ready')
    _write(project, 'tests/conftest.py',
           'import pytest\nfrom pathlib import Path\n'
           '@pytest.fixture(scope="session")\ndef base():\n'
           '    return Path("data/session.txt").read_text()\n'
           '@pytest.fixture\ndef consumer(request):\n'
           '    return request.getfixturevalue("base")\n')
    for test in TESTS:
        _write(project, test, 'def test_cached(consumer):\n    assert consumer == "ready"\n')
    records = impact.records_from_observations(
        _observe(project, tmp_path, *extra), TESTS, _tree(project), 'tree', {})
    assert {record.test for record in records} == set(TESTS)
    for record in records:
        assert record.outcome == 'passed' and not record.opaque
        assert record.deps.get('data/session.txt') == ('blob-data/session.txt', None), record.test
        assert 'consumer' in record.deps['tests/conftest.py'][1], (
            'a fixture finalizer runs outside the test bucket, so its fixture is always an input')


def test_a_file_whose_items_did_not_all_execute_stays_unrecorded():
    lines = [{'kind': 'file', 'test': 'tests/test_a.py', 'units': {}, 'data': [], 'dirs': [],
              'opaque': False, 'failed': [], 'fixtures': [],
              'collected': ['tests/test_a.py::one', 'tests/test_a.py::two'],
              'executed': ['tests/test_a.py::one']}]
    tree = impact.Tree({'tests/test_a.py': 'blob'})

    assert impact.records_from_observations(lines, ['tests/test_a.py'], tree, 'tree', {}) == []
    lines[0]['executed'].append('tests/test_a.py::two')
    assert len(impact.records_from_observations(lines, ['tests/test_a.py'], tree, 'tree', {})) == 1


def test_execution_receipts_survive_unavailable_monitoring_without_cache_records(tmp_path):
    from lib.project_mod.landing_verification import _execution_coverage
    project = tmp_path / 'project'
    _project(project)
    _write(project, 'occupied_monitoring.py',
           'import sys\n'
           'sys.monitoring.use_tool_id(3, "test-occupied-three")\n'
           'sys.monitoring.use_tool_id(4, "test-occupied-four")\n')
    lines = _observe(project, tmp_path, '-p', 'occupied_monitoring')
    collected, executed = _execution_coverage(lines)
    assert 'tests/test_b.py::test_fails' in executed
    assert collected == executed
    assert {line['kind'] for line in lines} == {'selection'}
    assert impact.records_from_observations(lines, TESTS, _tree(project), 'tree', REFS) == []


def git(cwd, *args) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    result = subprocess.run(['git', '-C', str(cwd), *args], env=env, text=True,
                            capture_output=True, timeout=60)
    assert result.returncode == 0, (args, result.stderr)
    return result.stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init', '-q', '-b', 'trunk')
    git(root, 'config', 'user.name', 'Impact')
    git(root, 'config', 'user.email', 'impact@example.invalid')
    _write(root, 'pkg/calc.py', BASE_SOURCE)
    _write(root, 'data/a.json', '{}\n')
    _write(root, 'tests/test_calc.py', 'from pkg.calc import helper\n')
    git(root, 'add', '-A')
    git(root, 'commit', '-qm', 'base')
    return root


def _commit(root: Path, relative: str, text: str) -> impact.Tree:
    _write(root, relative, text)
    git(root, 'add', '-A')
    git(root, 'commit', '-qm', f'change {relative}')
    return impact.Tree.of(root, 'HEAD')


def _record(root: Path, units: list[str], *, outcome='passed', opaque=False) -> impact.Record:
    tree = impact.Tree.of(root, 'HEAD')
    return impact.Record(
        'tests/test_calc.py', git(root, 'rev-parse', 'HEAD^{tree}').strip(), outcome,
        ['tests/test_calc.py::test_x'] if outcome == 'failed' else [], opaque,
        {'pkg/calc.py': (tree.blobs['pkg/calc.py'], units),
         'tests/test_calc.py': (tree.blobs['tests/test_calc.py'], None)},
        {'data': tree.listing('data')})


def test_only_the_units_a_record_names_and_its_listings_affect_it(repo, tmp_path):
    store = impact.ImpactStore(tmp_path / 'store', 'env')
    executed = _record(repo, ['<module>', 'helper'])
    import_time_only = _record(repo, ['helper'])

    other_function = _commit(repo, 'pkg/calc.py', BASE_SOURCE.replace('return LIMIT', 'return 0'))
    assert impact.affected(executed, other_function, store, repo) == ''
    helper_body = _commit(repo, 'pkg/calc.py', BASE_SOURCE.replace('return inner()', 'return 2'))
    assert impact.affected(executed, helper_body, store, repo) == 'pkg/calc.py'
    module = _commit(repo, 'pkg/calc.py', BASE_SOURCE.replace('LIMIT = 1', 'LIMIT = 2'))
    assert impact.affected(executed, module, store, repo) == 'pkg/calc.py'
    assert impact.affected(import_time_only, module, store, repo) == '', (
        'a function that only ran at import time does not make the module body a dependency')
    git(repo, 'checkout', '-q', 'HEAD~3', '--', 'pkg/calc.py')
    listed = _commit(repo, 'data/b.json', '{}\n')
    assert impact.affected(executed, listed, store, repo) == 'data'


def test_the_plan_runs_what_records_cannot_vouch_for(repo, tmp_path):
    store = impact.ImpactStore(tmp_path / 'store', 'env')
    tip = impact.Tree.of(repo, 'HEAD')
    records = {'tests/test_calc.py': _record(repo, ['<module>', 'helper'])}
    tests = ['tests/test_calc.py', 'tests/test_new.py', 'tests/test_far.py']
    static = {'tests/test_calc.py': 'direct', 'tests/test_new.py': 'direct'}

    quiet = impact.plan(records, tests, static, tip, tip, store, repo)

    assert quiet.run == {'tests/test_new.py': 'not yet recorded; direct'}
    assert (quiet.skipped, quiet.recorded) == (1, 1)

    failing = {'tests/test_calc.py': _record(repo, ['<module>', 'helper'], outcome='failed')}
    fixed_on_trunk = _commit(repo, 'pkg/calc.py', BASE_SOURCE.replace('return inner()', 'return 3'))
    unchanged = impact.plan(failing, tests, {}, tip, tip, store, repo)
    reverted = impact.plan(failing, tests, {}, fixed_on_trunk, tip, store, repo)
    assert unchanged.run == {}, 'it fails on both sides: preexisting'
    assert list(reverted.run) == ['tests/test_calc.py'], 'trunk differs, so it may pass there'

    guard = {'tests/test_calc.py': 'guard-core'}
    observed = {'tests/test_calc.py': _record(repo, ['<module>', 'helper'])}
    opaque = {'tests/test_calc.py': _record(repo, ['<module>', 'helper'], opaque=True)}
    assert impact.plan(observed, tests, guard, fixed_on_trunk, fixed_on_trunk, store,
                       repo).run == {}, 'a guard whose whole run was observed is answered by it'
    assert list(impact.plan(opaque, tests, guard, fixed_on_trunk, fixed_on_trunk, store,
                            repo).run) == ['tests/test_calc.py'], (
        'a guard that starts unobserved programs always runs: what they read is unknown')
    assert list(impact.plan(opaque, tests, {'tests/test_calc.py': 'direct'},
                           fixed_on_trunk, fixed_on_trunk, store, repo).run) == ['tests/test_calc.py']
    assert list(impact.plan({}, tests, guard, fixed_on_trunk, fixed_on_trunk, store,
                            repo).run) == ['tests/test_calc.py']


def test_import_initialization_outside_the_snapshot_is_opaque():
    test = 'tests/test_calc.py'
    node = test + '::test_x'
    observations = [
        {'kind': 'file', 'test': test, 'units': {'pkg/calc.py': ['scale']},
         'collected': [node], 'executed': [node]},
        {'kind': 'import-time', 'module': 'pkg/calc.py', 'units': {'generated.py': ['build']}},
    ]
    tree = impact.Tree({test: 'test-blob', 'pkg/calc.py': 'source-blob'})
    records = impact.records_from_observations(observations, [test], tree, 'tree', {})
    assert len(records) == 1 and records[0].opaque


def test_opaque_failure_cannot_stand_in_for_a_current_baseline_run(tmp_path):
    from lib.project_mod.landing_verification import _known_at_tip
    test = 'tests/test_calc.py'
    node = test + '::test_x'
    record = impact.Record(test, 'tree', 'failed', [node], True, {}, {})
    assert _known_at_tip((node,), {test: record}, impact.Tree({}), None, tmp_path) == ({}, [node])


def test_records_hold_for_one_environment_and_fingerprints_for_ever(repo, tmp_path, monkeypatch):
    directory = tmp_path / 'store'
    store = impact.ImpactStore(directory, 'env-a')
    store.save([_record(repo, ['<module>', 'helper'])])
    blob = impact.Tree.of(repo, 'HEAD').blobs['pkg/calc.py']
    fingerprints = store.fingerprints(repo, blob)
    assert fingerprints
    store.close()
    assert store.path == directory / f'impact-v{SCHEMA_VERSION}.sqlite', (
        'one file per schema: code versions running side by side keep their own records')

    same = impact.ImpactStore(directory, 'env-a')
    assert list(same.records()) == ['tests/test_calc.py']
    same.close()
    other = impact.ImpactStore(directory, 'env-b')
    assert other.records() == {}
    def no_git_read(*args):
        pytest.fail('a persisted fingerprint must not reread the Git blob')

    monkeypatch.setattr(impact, '_git', no_git_read)
    assert other.fingerprints(repo, blob) == fingerprints
    other.close()


def test_global_inputs_bind_every_record_they_scope(repo, tmp_path):
    for relative, text in (('settings.toml', 'level = 1\n'), ('tests/_harness.py', 'HARNESS = 1\n'),
                           ('tests/test_frontend_view.py', 'def test_view():\n    pass\n')):
        _write(repo, relative, text)
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', 'global inputs')
    tests = ['tests/test_calc.py', 'tests/test_frontend_view.py']
    lines = [{'kind': 'file', 'test': test, 'units': {}, 'data': [], 'dirs': [], 'opaque': False,
              'failed': [], 'fixtures': [], 'collected': [f'{test}::t'], 'executed': [f'{test}::t']}
             for test in tests]
    scope = {'settings.toml': 'ALL', 'tests/_harness.py': 'test_frontend_'}

    calc, view = impact.records_from_observations(
        lines, tests, impact.Tree.of(repo, 'HEAD'), 'tree', {}, scope)

    assert calc.deps['settings.toml'][1] is None and 'tests/_harness.py' not in calc.deps
    assert view.deps['tests/_harness.py'][1] == impact.ALL_UNITS
    store = impact.ImpactStore(tmp_path / 'store', 'env')
    commented = _commit(repo, 'tests/_harness.py', '# Layout only.\nHARNESS = 1\n')
    assert impact.affected(view, commented, store, repo) == ''
    recoded = _commit(repo, 'tests/_harness.py', 'HARNESS = 2\n')
    assert impact.affected(view, recoded, store, repo) == 'tests/_harness.py'
    assert impact.affected(calc, recoded, store, repo) == ''
    retuned = _commit(repo, 'settings.toml', 'level = 2\n')
    assert impact.affected(calc, retuned, store, repo) == 'settings.toml'


def test_same_code_ignores_comments_and_layout_only(repo, tmp_path):
    store = impact.ImpactStore(tmp_path / 'store', 'env')
    before = impact.Tree.of(repo, 'HEAD').blobs['pkg/calc.py']
    commented = _commit(repo, 'pkg/calc.py', '# A note.\n' + BASE_SOURCE + '\n\n').blobs['pkg/calc.py']
    recoded = _commit(repo, 'pkg/calc.py',
                      BASE_SOURCE.replace('LIMIT = 1', 'LIMIT = 3')).blobs['pkg/calc.py']

    assert impact.same_code(repo, before, commented, store)
    assert impact.same_code(repo, before, commented, None)
    assert not impact.same_code(repo, before, recoded, store)
    assert not impact.same_code(repo, before, None, store)


class _FingerprintStore:
    def __init__(self, units):
        self.units = units

    def fingerprints(self, root, blob):
        return self.units[blob]


def test_an_inert_signature_belongs_to_the_function_not_its_importers():
    source = 'from __future__ import annotations\n\ndef helper(x: Missing = None) -> None:\n    return x\n'
    before = impact.unit_fingerprints(source)
    signature = impact.unit_fingerprints(source.replace('x: Missing = None', 'x: Missing = 1'))
    body = impact.unit_fingerprints(source.replace('return x', 'return None'))
    assert signature['helper'] != before['helper']
    assert signature['<module>'] == before['<module>'], 'only a constant default changed'
    assert body['<module>'] == before['<module>']

    for edited in (
            source.replace('def helper', '@register\ndef helper'),
            source.replace('x: Missing = None', 'x=lookup()'),
            source.replace('from __future__ import annotations\n', ''),
            source.replace('def helper', 'def other')):
        changed = impact.unit_fingerprints(edited)
        assert changed['<module>'] != before['<module>'], edited.splitlines()[-1]


def test_conftest_session_code_is_a_shared_input_and_unused_helpers_are_not():
    source = ('from __future__ import annotations\nimport pytest\n'
              'from tests.support import cleanup\n\n'
              'def _session_helper():\n    return 1\n\n\n'
              'def _unused():\n    return 2\n\n\n'
              'def pytest_sessionstart():\n    _session_helper()\n\n\n'
              '@pytest.fixture(autouse=True)\ndef _clean():\n    cleanup.retire()\n    yield\n')
    shared = impact.conftest_units(source, 'tests/conftest.py')
    assert impact.CONFTEST_UNIT in shared
    assert '_session_helper' in shared, (
        'a hook may finish outside the test bucket, so its helper stays an input')
    assert 'cleanup' not in shared, 'external modules have their own records'
    assert '_unused' not in shared
    assert '_clean' in shared, 'the autouse fixture runs at collection for every file'

    record = impact.Record('tests/test_a.py', 'tree', 'passed', [], False,
                           {'tests/conftest.py': ('before', sorted(shared | {impact.MODULE_UNIT}))}, {})
    tree = impact.Tree({'tests/conftest.py': 'after', 'tests/test_a.py': 'test'})
    store = _FingerprintStore({'before': impact.unit_fingerprints(source),
                               'after': impact.unit_fingerprints(source)})
    assert impact.affected(record, tree, store, '.') == ''
    unused = source.replace('return 2', 'return 3')
    store.units['after'] = impact.unit_fingerprints(unused)
    assert impact.affected(record, tree, store, '.') == '', 'an unused helper changed only its own unit'
    for old, new in (('return 1', 'return 0'), ('_session_helper()', 'pass'),
                     ('cleanup.retire()', 'cleanup.other()')):
        store.units['after'] = impact.unit_fingerprints(source.replace(old, new))
        assert impact.affected(record, tree, store, '.') == 'tests/conftest.py', new


def test_a_conftest_helper_reached_at_import_time_is_shared():
    """The shape this repository's own conftest has: a shim installed at import."""
    source = ('def _client():\n    return object()\n\n\n'
              'def _install_shim():\n    return _client()\n\n\n'
              '_CLIENT = _install_shim()\n')
    shared = impact.conftest_units(source, 'tests/conftest.py')
    assert {'_install_shim', '_client'} <= shared, (
        'import-time setup runs for every collected file, so its helpers are shared inputs')
    record = impact.Record('tests/test_a.py', 'tree', 'passed', [], False,
                           {'tests/conftest.py': ('before', sorted(shared | {impact.MODULE_UNIT}))}, {})
    tree = impact.Tree({'tests/conftest.py': 'after'})
    store = _FingerprintStore({'before': impact.unit_fingerprints(source),
                               'after': impact.unit_fingerprints(
                                   source.replace('return object()', 'return dict()'))})
    assert impact.affected(record, tree, store, '.') == 'tests/conftest.py'
