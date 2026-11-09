"""The default development entrypoint verifies frozen changes through one engine."""
from pathlib import Path
import os
import subprocess
import sys

import pytest

from lib.project_mod.verification import main, verify_tests
from tests.project_mod.test_landing_verification import (
    commit, git, private_local_root as private_local_root, shared as shared, write,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
_AUDIT_SYNTHETIC_REPO_PATHS = {
    'tests/test_calc.py', 'tests/test_other.py', 'tests/test_new.py', 'tests/test_guard.py',
}


def test_unchanged_checkout_does_not_claim_a_test_pass(shared, monkeypatch):
    from lib.project_mod import landing_verification
    monkeypatch.setattr(landing_verification, 'merge_checkout',
                        lambda *args, **kwargs: pytest.fail('no changes need no checkout'))
    result = verify_tests(shared, changed=True)
    assert result.status == 'unchanged'
    assert result.selected == result.executed == result.reused == ()


def test_changes_select_dependents_and_new_tests_from_the_frozen_tree(shared):
    write(shared, 'tests/test_other.py', 'def test_other():\n    assert False\n')
    commit(shared, 'unrelated failure')
    write(shared, 'pkg/calc.py', 'def scale(x):\n    return x * 3\n')
    write(shared, 'tests/test_new.py', 'def test_new():\n    assert True\n')
    result = verify_tests(shared, changed=True)
    assert result.status == 'failed'
    assert result.selected == ('tests/test_calc.py', 'tests/test_new.py')
    assert set(result.changed) == {'pkg/calc.py', 'tests/test_new.py'}
    assert result.failed_cases == ('tests/test_calc.py::test_scale',)


def test_changed_scope_includes_recorded_transitive_inputs(shared):
    write(shared, 'pkg/constant.py', 'def factor():\n    return 2\n')
    write(shared, 'pkg/calc.py', 'from pkg.constant import factor\ndef scale(x):\n    return x * factor()\n')
    commit(shared, 'indirect dependency')
    assert verify_tests(shared, ['tests/test_calc.py']).status == 'passed'
    write(shared, 'pkg/constant.py', 'def factor():\n    return 3\n')
    result = verify_tests(shared, changed=True)
    assert result.status == 'failed'
    assert result.selected == ('tests/test_calc.py',)
    assert 'pkg/constant.py' in result.selection_reasons['tests/test_calc.py']


def test_changed_scope_reuses_complete_evidence_for_unchanged_code(shared, monkeypatch):
    from lib.project_mod import landing_verification
    assert verify_tests(shared, ['tests/test_calc.py']).status == 'passed'
    write(shared, 'pkg/calc.py', '# comment\ndef scale(x):\n    return x * 2\n')
    monkeypatch.setattr(landing_verification, '_run',
                        lambda *args, **kwargs: pytest.fail('matching evidence must be reused'))
    result = verify_tests(shared, changed=True)
    assert result.status == 'passed' and result.reused == ('tests/test_calc.py',)


def test_base_selection_includes_commits_and_working_bytes(shared):
    base = git(shared, 'rev-parse', 'HEAD').strip()
    write(shared, 'pkg/calc.py', 'def scale(x):\n    return x + x\n')
    commit(shared, 'committed change')
    write(shared, 'tests/test_new.py', 'def test_new():\n    assert False\n')
    result = verify_tests(shared, changed=True, base=base)
    assert result.status == 'failed'
    assert set(result.changed) == {'pkg/calc.py', 'tests/test_new.py'}
    assert result.base == base


def test_deleted_source_keeps_importing_tests_in_scope(shared):
    (shared / 'pkg/calc.py').unlink()
    result = verify_tests(shared, changed=True, execute=False)
    assert result.status == 'planned'
    assert result.selected == ('tests/test_calc.py',)


def test_changed_selection_preserves_the_selectors_mandatory_guards(shared):
    write(shared, 'tests/test_guard.py', 'def test_guard():\n    assert True\n')
    selector = shared / 'scripts/test_select.py'
    selector.write_text(selector.read_text() + '\noriginal_select = select_tests\n'
        'def select_tests(index, changed):\n'
        '    selected, reasons = original_select(index, changed)\n'
        '    reasons["tests/test_guard.py"] = "guard-core"\n'
        '    return set(reasons), reasons\n')
    commit(shared, 'mandatory guard')
    write(shared, 'notes.txt', 'unreferenced edit')
    result = verify_tests(shared, changed=True)
    assert result.status == 'passed'
    assert result.executed == ('tests/test_guard.py',)
    assert result.selection_reasons == {'tests/test_guard.py': 'guard-core'}


def test_unmatched_changes_are_explicitly_advisory(shared):
    write(shared, 'notes.txt', 'unreferenced edit')
    result = verify_tests(shared, changed=True)
    assert result.status == 'unselected' and result.executed == ()


def test_invalid_base_or_mixed_scopes_cannot_run_a_different_selection(shared):
    with pytest.raises(ValueError):
        verify_tests(shared, ['tests/test_calc.py'], changed=True)
    assert main(['run', '--into', str(shared), '--changed', '--base', 'does-not-exist']) == 2


def _make(root, *arguments):
    return subprocess.run(['make', '--no-print-directory', *arguments], cwd=root,
                          env={**os.environ, 'PYTHONPATH': str(ROOT)},
                          text=True, capture_output=True, timeout=60)


def test_make_default_and_explicit_scopes_use_the_verification_engine(shared):
    write(shared, 'Makefile', (ROOT / 'Makefile').read_text())
    write(shared, 'scripts/verify.py', (ROOT / 'scripts/verify.py').read_text())
    python = shared / '.venv/bin/python3'
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    commit(shared, 'entrypoints')
    empty = _make(shared, 'test')
    assert empty.returncode == 0 and 'Verification unchanged:' in empty.stdout, empty.stdout + empty.stderr
    alias = _make(shared, 'test-affected')
    assert alias.returncode == 0 and 'Verification unchanged:' in alias.stdout, alias.stdout + alias.stderr
    write(shared, 'pkg/calc.py', 'def scale(x):\n    return x * 3\n')
    plan = _make(shared, 'test-plan')
    assert plan.returncode == 0 and 'Verification planned:' in plan.stdout, plan.stdout + plan.stderr
    assert 'tests/test_calc.py' in plan.stdout
    failed = _make(shared, 'test', 'TESTS=tests/test_calc.py')
    assert failed.returncode != 0 and 'Verification failed:' in failed.stdout, failed.stdout + failed.stderr


def test_full_and_ci_gates_keep_unconditional_execution():
    for target in ('test-all', 'test-full', 'ci'):
        result = _make(ROOT, '-n', target, 'JOBS=0')
        assert result.returncode == 0, result.stderr
        assert '-m pytest' in result.stdout
        assert 'scripts/verify.py' not in result.stdout
