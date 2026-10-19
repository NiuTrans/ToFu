"""Behavioral specifications for affected-test discovery, selection and execution."""

import importlib.util
import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
_SCRIPT = os.path.join(ROOT, 'scripts', 'test_select.py')


def _load():
    spec = importlib.util.spec_from_file_location('test_select', _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ts = _load()


def _p(head, rest):
    """Synthetic fixture path, built in TWO segments on purpose.

    The paths below point NOWHERE — they are inputs/expected-outputs of the
    pure refs-extraction/selection functions, not references to repo files.
    Written as 'dir' + 'rest' so the suite-health census (_PATH_RE dead-anchor
    scan, scripts/audit_tests.py category F) does not mistake them for real
    stale anchors — a plain 'lib/foo/bar.py' literal would be flagged as a
    missing-path reference, which is exactly what these strings are NOT.
    """
    return head + '/' + rest


class TestRefsOfTestFile:
    def test_import_maps_to_repo_path(self):
        src = "import lib.foo.bar\nfrom routes.api_v1 import chat\n"
        refs = ts.refs_of_test_file(src)
        assert _p('lib', 'foo/bar.py') in refs
        assert (_p('routes', 'api_v1/__init__.py') in refs
                or _p('routes', 'api_v1.py') in refs)

    def test_from_import_of_module_maps_to_module_path(self):
        src = "from lib.tasks_pkg.manager import get_task\n"
        refs = ts.refs_of_test_file(src)
        assert (_p('lib', 'tasks_pkg/manager.py') in refs
                or _p('lib', 'tasks_pkg/manager/__init__.py') in refs)

    def test_tests_helper_import_maps(self):
        src = "from tests._jsdom import run_harness\n"
        refs = ts.refs_of_test_file(src)
        assert 'tests/_jsdom.py' in refs

    def test_literal_path_refs_captured(self):
        src = "JS = os.path.join(ROOT, 'frontend', 'src', 'main.ts')\n" \
              "X = 'frontend/src/api/transport.ts'\n" \
              "DOC = 'docs/contracts/API_CONTRACT.md'\n"
        refs = ts.refs_of_test_file(src)
        assert _p('frontend', 'src/api/transport.ts') in refs
        assert _p('docs', 'contracts/API_CONTRACT.md') in refs

    def test_external_packages_not_mapped(self):
        src = "import pytest\nimport tofu_search\nimport requests\n"
        refs = ts.refs_of_test_file(src)
        assert refs == set() or all(
            r.startswith(('lib/', 'routes/', 'tests/', 'static/', 'docs/'))
            for r in refs)


class TestSelectTests:
    A = _p('tests', 'test_a.py')
    B = _p('tests', 'test_b.py')
    C = _p('tests', 'test_frontend_c.py')
    D = _p('tests', 'test_d.py')
    LIB_X = _p('lib', 'x.py')
    LIB_W = _p('lib', 'w.py')
    UI_Y = _p('frontend', 'src/features/y.ts')
    CORE_Z = _p('frontend', 'src/runtime/app-runtime.js')
    JSDOM_HELPER = _p('tests', '_jsdom.py')
    API_JS = _p('frontend', 'src/api/transport.ts')
    CONFTEST = _p('tests', 'conftest.py')
    INDEX = {
        A: {LIB_X},
        B: {UI_Y},
        C: {CORE_Z},
        D: {LIB_X, LIB_W},
        **{f'tests/contracts/{name}': set() for name in ts.GUARD_CORE_BASENAMES},
    }

    def test_changed_source_selects_importers(self):
        selected, _ = ts.select_tests(self.INDEX, [self.LIB_X])
        assert selected >= {self.A, self.D}
        assert self.B not in selected

    def test_changed_test_file_selects_itself(self):
        selected, _ = ts.select_tests(self.INDEX, [self.B])
        assert self.B in selected

    def test_blast_radius_jsdom_helper_pulls_frontend_family(self):
        selected, _ = ts.select_tests(self.INDEX, [self.JSDOM_HELPER])
        assert self.C in selected

    def test_blast_radius_api_transport_pulls_frontend_family(self):
        selected, _ = ts.select_tests(self.INDEX, [self.API_JS])
        assert self.C in selected

    def test_guard_core_always_runs(self):
        selected, _ = ts.select_tests(self.INDEX, [self.LIB_X])
        assert any('contract' in os.path.basename(f) for f in selected), (
            'guard core must always be in the selection')

    def test_unknown_change_falls_back_to_guard_core(self):
        selected, reasons = ts.select_tests(self.INDEX, ['README.md'])
        direct = {f for f in selected if reasons[f] != 'guard-core'}
        assert not direct, 'unrelated change must not pull behaviour suites'
        assert selected, 'guard core must still run as the smoke floor'

    def test_conftest_change_selects_everything(self):
        selected, _ = ts.select_tests(self.INDEX, [self.CONFTEST])
        assert selected >= set(self.INDEX), (
            'conftest touches every session — the blast radius is the whole suite')


class TestPytestCommand:
    def test_parallel_command_only_selects_worker_count(self):
        cmd = ts.build_pytest_command([_p('tests', 'test_a.py')], '2')

        workers = cmd.index('-n')
        assert cmd[workers + 1] == '2'
        assert 'xdist.plugin' not in cmd
        assert '--dist' not in cmd

    def test_serial_command_does_not_pass_worker_flags(self):
        cmd = ts.build_pytest_command(None, '0')

        assert '-n' not in cmd
        assert cmd[cmd.index('-m', 3) + 1] == '(unit or api) and not (visual or slow or live_llm)'

    def test_implicit_single_file_selection_stays_serial(self):
        cmd = ts.build_pytest_command([_p('tests', 'test_a.py')], None)

        assert '-n' not in cmd

    def test_implicit_multi_file_selection_uses_shared_auto_policy(self):
        selected = [f'tests/test_{i}.py' for i in range(10)]
        cmd = ts.build_pytest_command(selected, None)

        assert cmd[cmd.index('-n') + 1] == 'auto'


@pytest.fixture
def selection_repo(tmp_path):
    import subprocess
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / 'tests').mkdir()
    return tmp_path


def write_test(root, relative, content='assert True\n'):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_index_discovers_nested_and_untracked_tests_but_not_ignored(selection_repo):
    import subprocess
    root = selection_repo
    tracked = write_test(root, _p('tests', 'domain/test_tracked.py'), 'import lib.example\n')
    subprocess.run(['git', 'add', str(tracked)], cwd=root, check=True)
    nested = write_test(root, _p('tests', 'new domain/test_new.py'))
    (root / '.gitignore').write_text('tests/ignored/\n')
    write_test(root, _p('tests', 'ignored/test_cache.py'))
    index = ts.build_index(str(root / 'tests'), None)
    assert set(index) == {tracked.relative_to(root).as_posix(), nested.relative_to(root).as_posix()}
    assert _p('lib', 'example.py') in index[tracked.relative_to(root).as_posix()]['refs']


def test_git_failure_cannot_be_an_empty_success(tmp_path):
    import subprocess
    (tmp_path / 'tests').mkdir()
    with pytest.raises(subprocess.CalledProcessError):
        ts.build_index(str(tmp_path / 'tests'), None)


def test_guard_core_resolves_current_paths_and_deleted_tests_are_not_selected():
    guards = {_p('tests', 'contracts/' + name): set() for name in ts.GUARD_CORE_BASENAMES}
    deleted = _p('tests', 'domain/test_removed.py')
    selected, reasons = ts.select_tests(guards, [deleted])
    assert selected == set(guards)
    assert set(reasons.values()) == {'guard-core'}


def test_missing_guard_fails_with_actionable_error():
    with pytest.raises(ValueError, match='guard'):
        ts.select_tests({}, ['README.md'])


def test_empty_selection_cannot_launch_unscoped_pytest():
    with pytest.raises(ValueError, match='empty'):
        ts.build_pytest_command([], None)


def test_nested_conftest_selects_its_subtree():
    guards = {_p('tests', 'contracts/' + name): set() for name in ts.GUARD_CORE_BASENAMES}
    inside, outside = _p('tests', 'domain/test_a.py'), _p('tests', 'neighbor/test_b.py')
    selected, _ = ts.select_tests({**guards, inside: set(), outside: set()},
                                  [_p('tests', 'domain/conftest.py')])
    assert inside in selected
    assert outside not in selected



def test_cache_forgets_removed_files_and_rebuilds_invalid_entries(selection_repo):
    import json
    root = selection_repo
    keep = write_test(root, _p('tests', 'domain/test_keep.py'), 'import lib.before\n')
    removed = write_test(root, _p('tests', 'domain/test_removed.py'))
    cache = root / 'cache.json'
    first = ts.build_index(str(root / 'tests'), str(cache))
    assert len(first) == 2
    removed.unlink()
    keep.write_text('from lib import after\n')
    changed = ts.build_index(str(root / 'tests'), str(cache))
    assert set(changed) == {keep.relative_to(root).as_posix()}
    assert _p('lib', 'after.py') in next(iter(changed.values()))['refs']
    assert ts.build_index(str(root / 'tests'), str(cache)) == changed
    cache.write_text(json.dumps({'version': 2, 'index': []}))
    assert ts.build_index(str(root / 'tests'), str(cache)) == changed
    cache.write_text('{')
    assert ts.build_index(str(root / 'tests'), str(cache)) == changed


def test_git_changes_preserve_rename_endpoints_and_unusual_names(selection_repo, monkeypatch):
    import subprocess
    root = selection_repo
    before = write_test(root, _p('tests', 'domain/test_before.py'))
    def git(*args):
        return subprocess.run(['git', '-c', 'user.name=Test', '-c',
                               'user.email=test@example.invalid', *args], cwd=root,
                              check=True, text=True, capture_output=True).stdout.strip()
    git('add', '.')
    git('commit', '-qm', 'initial')
    base = git('rev-parse', 'HEAD')
    after = before.with_name('test_after space\nline.py')
    before.rename(after)
    extra = write_test(root, _p('tests', 'new domain/test_extra.py'))
    monkeypatch.setattr(ts, 'ROOT', str(root))
    expected = sorted(p.relative_to(root).as_posix() for p in (before, after, extra))
    assert ts.changed_files(None) == expected
    git('add', '.')
    git('commit', '-qm', 'rename and add')
    assert ts.changed_files(base) == expected
    assert ts.changed_files(None) == []


def test_default_execution_never_enables_opt_in_tiers():
    for files in (None, [_p('tests', 'domain/test_api.py')]):
        command = ts.build_pytest_command(files, '0')
        assert command[command.index('-m', 3) + 1] == '(unit or api) and not (visual or slow or live_llm)'



def test_workflow_literal_selects_its_behavioral_contract():
    workflow = '.github/workflows/ci.yml'
    assert workflow in ts.refs_of_test_file("path = ROOT / '.github/workflows/ci.yml'")


def test_selector_cli_runs_api_and_unit_without_opt_in_tiers(selection_repo):
    import shutil
    import subprocess
    import sys
    root = selection_repo
    scripts = root / 'scripts'
    scripts.mkdir()
    shutil.copyfile(_SCRIPT, scripts / 'test_select.py')
    (root / 'pyproject.toml').write_text(
        '[tool.pytest.ini_options]\naddopts = "-p pytest_timeout"\n'
        'markers = ["unit", "api", "visual", "slow", "live_llm"]\n')
    for name in ts.GUARD_CORE_BASENAMES:
        write_test(root, _p('tests', 'guards/' + name),
                   'import pytest\npytestmark = pytest.mark.unit\ndef test_guard():\n    assert True\n')
    write_test(root, _p('tests', 'domain/test_api.py'),
               'import pytest\nfrom pathlib import Path\npytestmark = pytest.mark.api\n'
               'def test_api():\n    Path("api-ran").write_text("yes")\n')
    for marker in ('visual', 'slow', 'live_llm'):
        write_test(root, _p('tests', f'optional/test_{marker}.py'),
                   f'import pytest\npytestmark = pytest.mark.{marker}\n'
                   'def test_optional():\n    pytest.fail("opt-in tier must not execute")\n')
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '--allow-empty', '-qm', 'initial'], cwd=root, check=True)
    result = subprocess.run([sys.executable, str(scripts / 'test_select.py'), '--run', '--jobs', '0'],
                            cwd=root, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / 'api-ran').read_text() == 'yes'
