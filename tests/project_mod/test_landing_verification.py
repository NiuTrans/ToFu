"""Landing verification: git_land refuses merged results that newly fail tests.

Every test builds a small repository under tmp_path with its own
``scripts/test_select.py`` (the selector the verification loads from the
merged tree) and real pytest tests, then lands a worktree branch through
``scripts/git_land.py``. Nothing touches the checkout running the suite.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts' / 'git_land.py'

SELECTOR = '''\
import ast, os, sys

def build_index(tests_dir, cache_path=None):
    index = {}
    for name in sorted(os.listdir(tests_dir)):
        if name.startswith('test_') and name.endswith('.py'):
            with open(os.path.join(tests_dir, name)) as handle:
                tree = ast.parse(handle.read())
            refs = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    refs.add(node.module.replace('.', '/') + '.py')
            index['tests/' + name] = {'refs': sorted(refs)}
    return index

def select_tests(index, changed):
    chosen = {test: 'direct' for test, record in index.items()
              if test in changed or set(record['refs']) & set(changed)}
    return set(chosen), chosen

def build_pytest_command(existing, jobs):
    return [sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider', *existing, '-q']
'''


def _load():
    spec = importlib.util.spec_from_file_location('git_land_verification_under_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


land = _load()


def git(cwd, *args) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    result = subprocess.run(['git', '-C', str(cwd), *args], env=env, text=True,
                            capture_output=True, timeout=60)
    assert result.returncode == 0, (args, result.stderr)
    return result.stdout


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def commit(root: Path, message: str) -> None:
    git(root, 'add', '-A')
    git(root, 'commit', '-qm', message)


@pytest.fixture(autouse=True)
def private_local_root(tmp_path: Path, monkeypatch):
    """The warm merge checkout and test records stay inside this test's directory."""
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('TOFU_LOCAL_DIR', str(tmp_path / 'local'))


@pytest.fixture()
def shared(tmp_path: Path) -> Path:
    repo = tmp_path / 'shared'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'trunk')
    git(repo, 'config', 'user.name', 'Lander')
    git(repo, 'config', 'user.email', 'lander@example.invalid')
    write(repo, '.gitignore', '__pycache__/\n')
    write(repo, 'scripts/test_select.py', SELECTOR)
    write(repo, 'pkg/calc.py', 'def scale(x):\n    return x * 2\n')
    write(repo, 'tests/test_calc.py',
          'from pkg.calc import scale\n\ndef test_scale():\n    assert scale(2) == 4\n')
    commit(repo, 'base')
    return repo


@pytest.fixture()
def feature(shared: Path, tmp_path: Path) -> Path:
    path = tmp_path / 'feature'
    git(shared, 'worktree', 'add', '-q', '-b', 'feature', str(path))
    return path


def test_a_concurrent_trunk_change_that_breaks_the_branch_refuses(shared, feature):
    write(feature, 'pkg/report.py',
          'from pkg.calc import scale\n\ndef report(x):\n    return str(scale(x))\n')
    write(feature, 'tests/test_report.py',
          'from pkg.report import report\n\ndef test_report():\n    assert report(2) == "4"\n')
    commit(feature, 'feat(report): render the scaled value')
    write(shared, 'pkg/calc.py', 'def scale(x):\n    return x * 3\n')
    write(shared, 'tests/test_calc.py',
          'from pkg.calc import scale\n\ndef test_scale():\n    assert scale(2) == 6\n')
    commit(shared, 'fix(calc): triple')
    before = git(shared, 'rev-parse', 'HEAD')

    with pytest.raises(land.Refused) as refused:
        land.land('feature', into=str(shared))

    assert 'the merged result fails tests that pass on trunk' in refused.value.reason
    assert refused.value.paths == ['tests/test_report.py::test_report']
    assert 'Merge trunk into the worktree branch' in refused.value.advice
    assert git(shared, 'rev-parse', 'HEAD') == before
    assert (feature / 'tests' / 'test_report.py').exists()


def test_a_branch_that_breaks_a_concurrent_trunk_test_refuses(shared, feature):
    write(shared, 'pkg/report.py',
          'from pkg.calc import scale\n\ndef report(x):\n    return str(scale(x))\n')
    write(shared, 'tests/test_report.py',
          'from pkg.calc import scale\nfrom pkg.report import report\n\n'
          'def test_report():\n    assert report(2) == str(scale(1) * 2)\n'
          '\ndef test_report_literal():\n    assert report(2) == "4"\n')
    commit(shared, 'feat(report): render the scaled value')
    write(feature, 'pkg/calc.py', 'def scale(x):\n    return x * 3\n')
    write(feature, 'tests/test_calc.py',
          'from pkg.calc import scale\n\ndef test_scale():\n    assert scale(2) == 6\n')
    commit(feature, 'fix(calc): triple')

    with pytest.raises(land.Refused) as refused:
        land.land('feature', into=str(shared))
    assert refused.value.paths == ['tests/test_report.py::test_report_literal']


def test_failures_already_on_the_current_branch_do_not_block(shared, feature, capsys):
    write(shared, 'tests/test_calc.py',
          'from pkg.calc import scale\n\ndef test_scale():\n    assert scale(2) == 4\n'
          '\ndef test_known_broken():\n    assert scale(0) == 1\n')
    commit(shared, 'test(calc): a known broken case')
    write(feature, 'pkg/calc.py', 'def scale(x):\n    return x + x\n')
    commit(feature, 'refactor(calc): add instead of multiply')

    assert land.main(['feature', '--into', str(shared)]) == 0
    output = capsys.readouterr().out
    assert output.startswith('Landed feature on trunk: merge ')
    assert 'Integration check passed in ' in output
    assert 'only tests already failing on the current branch failed' in output
    assert ('Already failing on trunk, not caused by this branch: '
            'tests/test_calc.py::test_known_broken') in output


def test_a_failure_that_does_not_reproduce_is_reported_not_blocking(
        shared, feature, tmp_path, monkeypatch):
    monkeypatch.setenv('LANDING_FLAKE_MARKER', str(tmp_path / 'flaked-once'))
    write(feature, 'tests/test_flaky.py',
          'import os, pathlib\nfrom pkg.calc import scale\n\n'
          'def test_flaky():\n'
          '    marker = pathlib.Path(os.environ["LANDING_FLAKE_MARKER"])\n'
          '    first = not marker.exists()\n'
          '    marker.write_text("seen")\n'
          '    assert not first and scale(1) == 2\n')
    commit(feature, 'test(calc): flaky case')
    write(shared, 'README.md', 'trunk moved\n')
    commit(shared, 'docs: readme')

    result = land.land('feature', into=str(shared))
    assert result['status'] == 'landed'
    assert result['verification'].status == 'passed'
    assert result['verification'].flaky == ('tests/test_flaky.py::test_flaky',)


def test_a_fast_forward_landing_is_verified_too(shared, feature):
    write(feature, 'pkg/calc.py', 'def scale(x):\n    return x * 3\n')
    commit(feature, 'fix(calc): triple without updating its test')

    with pytest.raises(land.Refused) as refused:
        land.land('feature', into=str(shared))

    assert refused.value.paths == ['tests/test_calc.py::test_scale']


def test_a_repository_without_a_selector_lands_unverified_with_a_reason(shared, feature):
    git(shared, 'rm', '-q', 'scripts/test_select.py')
    commit(shared, 'chore: drop the selector')
    write(feature, 'pkg/calc.py', 'def scale(x):\n    return x * 3\n')
    commit(feature, 'fix(calc): triple')

    result = land.land('feature', into=str(shared))
    assert result['status'] == 'landed'
    assert result['verification'].status == 'skipped'
    assert 'declares no test selector' in result['verification'].reason


def test_terminating_a_landing_retires_its_test_session(shared, feature, tmp_path):
    import signal
    import time

    marker = tmp_path / 'hanging-test.pid'
    write(feature, 'tests/test_hang.py',
          'import os, pathlib, time\nfrom pkg.calc import scale\n\n'
          'def test_hang():\n'
          '    pathlib.Path(os.environ["LANDING_HANG_PID"]).write_text(str(os.getpid()))\n'
          '    time.sleep(600)\n')
    commit(feature, 'test(calc): a test that never finishes')
    write(shared, 'README.md', 'trunk moved\n')
    commit(shared, 'docs: readme')
    before = git(shared, 'rev-parse', 'HEAD')
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(PYTHONPATH=str(ROOT), LANDING_HANG_PID=str(marker))
    lander = subprocess.Popen([sys.executable, str(SCRIPT), 'feature', '--into', str(shared)],
                              env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 120
        while not marker.exists() and lander.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert marker.exists(), 'the selected test never started'
        test_pid = int(marker.read_text())
        lander.send_signal(signal.SIGTERM)
        assert lander.wait(timeout=60) == 128 + signal.SIGTERM
    finally:
        if lander.poll() is None:
            lander.kill()
            lander.wait()
    with pytest.raises(ProcessLookupError):
        os.kill(test_pid, 0)
    assert git(shared, 'rev-parse', 'HEAD') == before
    assert 'git-land-merge' not in git(shared, 'worktree', 'list', '--porcelain')


def _branch(shared: Path, tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    git(shared, 'worktree', 'add', '-q', '-b', name, str(path))
    return path


def test_a_recorded_test_file_is_skipped_until_code_it_executed_changes(shared, tmp_path):
    write(shared, 'pkg/other.py', 'def unused():\n    return 1\n\n\ndef helper():\n    return 7\n')
    write(shared, 'tests/test_other.py',
          'from pkg.other import helper\n\ndef test_helper():\n    assert helper() == 7\n')
    commit(shared, 'test(other): helper')
    from lib.project_mod.landing_verification import record_tests
    assert record_tests(shared)['recorded'] == 2

    unused = _branch(shared, tmp_path, 'unused')
    write(unused, 'pkg/other.py', 'def unused():\n    return 2\n\n\ndef helper():\n    return 7\n')
    commit(unused, 'refactor(other): a function no test executes')
    skipped = land.land('unused', into=str(shared))['verification']

    assert (skipped.status, skipped.selected, skipped.skipped_files) == ('passed', (), 2)
    assert 'no recorded test file used code or files the merge changes' in skipped.reason

    used = _branch(shared, tmp_path, 'used')
    write(used, 'pkg/other.py', 'def unused():\n    return 2\n\n\ndef helper():\n    return 8\n')
    commit(used, 'fix(other): helper returns eight')
    with pytest.raises(land.Refused) as refused:
        land.land('used', into=str(shared))
    assert refused.value.paths == ['tests/test_other.py::test_helper']


def test_a_failure_recorded_at_the_tip_is_preexisting_without_a_tip_run(
        shared, tmp_path, monkeypatch):
    write(shared, 'tests/test_calc.py',
          'from pkg.calc import scale\n\ndef test_scale():\n    assert scale(2) == 4\n'
          '\ndef test_known_broken():\n    assert scale(0) == 1\n')
    commit(shared, 'test(calc): a known broken case')
    from lib.project_mod import landing_verification
    assert landing_verification.record_tests(shared)['failed'] == 1
    lane = _branch(shared, tmp_path, 'lane')
    write(lane, 'pkg/calc.py', 'def scale(x):\n    return x + x\n')
    commit(lane, 'refactor(calc): add instead of multiply')

    def no_tip_run(*_args, **_kwargs):
        raise AssertionError('the record at the tip decides; no tip checkout is needed')

    monkeypatch.setattr(landing_verification, '_tip_failures', no_tip_run)
    verdict = land.land('lane', into=str(shared))['verification']

    assert verdict.status == 'passed'
    assert verdict.preexisting == ('tests/test_calc.py::test_known_broken',)


def test_a_module_body_change_reruns_every_test_file_that_imported_it(shared, feature):
    from lib.project_mod.landing_verification import record_tests
    record_tests(shared)
    write(feature, 'pkg/calc.py', 'FACTOR = 3\n\n\ndef scale(x):\n    return x * 2\n')
    commit(feature, 'feat(calc): a module constant')

    verdict = land.land('feature', into=str(shared))['verification']

    assert verdict.status == 'passed'
    assert verdict.selected == ('tests/test_calc.py',)


def test_the_merge_checkout_stays_warm_between_landings(shared, feature, tmp_path):
    from lib.project_mod.landing_verification import warm_checkout_path
    write(feature, 'pkg/calc.py', 'def scale(x):\n    return x + x\n')
    commit(feature, 'refactor(calc): add')
    land.land('feature', into=str(shared))
    warm = warm_checkout_path(shared)
    bytecode = warm / 'pkg' / '__pycache__'
    assert bytecode.is_dir(), 'bytecode compiled by the first run stays for the next one'
    (warm / 'stray.txt').write_text('left by a test\n')

    second = _branch(shared, tmp_path, 'second')
    write(second, 'pkg/calc.py', 'def scale(x):\n    return 2 * x\n')
    commit(second, 'refactor(calc): commute')
    assert land.land('second', into=str(shared))['status'] == 'landed'
    assert bytecode.is_dir()
    assert not (warm / 'stray.txt').exists(), 'everything else is a fresh checkout'
    assert len([line for line in git(shared, 'worktree', 'list', '--porcelain').splitlines()
                if line.startswith('worktree ')]) == 2
